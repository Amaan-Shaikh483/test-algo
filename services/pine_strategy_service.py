"""Server-side Pine strategy runner.

Owns the lifecycle of Pine strategy instances: historical warmup, realtime
candle aggregation, confirmed-bar evaluation, signal idempotency and routing
into OpenAlgo's existing execution pipeline.

Architecture (reuses the existing stack at every step):

    existing broker streaming adapter
            |
    existing ZeroMQ bus
            |
    existing WebSocket proxy (port 8765)
            |
    one internal WebSocketClient per user (existing services/websocket_client)
            |
    PineFeedDispatcher - shared candle aggregation per (symbol, exchange, tf)
            |
    StrategyRunner instances (one per strategy, in-process)
            |
    PineRuntime.process_bar(realtime=True)
            |
    signal (idempotent, persisted in pine_signals)
            |
    PAPER -> services.sandbox_service.sandbox_place_order (existing sandbox engine)
    LIVE  -> services.place_order_service.place_order (existing broker pipeline)

Concurrency: the WebSocketClient already runs its asyncio loop on a real OS
thread (its own eventlet workaround); confirmed-bar work is dispatched to a
small module-level ThreadPoolExecutor so one slow broker order never blocks
another strategy's ticks. No asyncio is introduced in the Flask/green-thread
path, so Gunicorn + eventlet + 1 worker stays safe.

Browser independence: once a strategy is RUNNING, everything above runs in the
backend process; closing the browser only detaches the Socket.IO listener.
"""

import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

from pine.backtest import run_backtest
from pine.compiler import compile_script
from pine.errors import PineError
from pine.runtime import Bar, PineRuntime, RuntimeConfig
from utils.event_bus import bus
from utils.logging import get_logger

logger = get_logger(__name__)

# Shared executor for confirmed-bar processing. Module-level singleton per the
# repository's FD/thread hygiene rules; small because runtime evaluation is
# milliseconds and only broker order calls are slow.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pine-runner")

_TIMEFRAME_RE = re.compile(r"^(\d+)([smh])$", re.IGNORECASE)


def interval_to_seconds(timeframe: str) -> int | None:
    """Map a chart interval token to seconds; None for D/W/M."""
    match = _TIMEFRAME_RE.match(timeframe or "")
    if not match:
        return None
    n = int(match.group(1))
    unit = match.group(2).lower()
    return n if unit == "s" else n * 60 if unit == "m" else n * 3600


def lookback_days(timeframe: str) -> int:
    """History lookback per interval, mirroring the /trading chart's choices."""
    seconds = interval_to_seconds(timeframe)
    if seconds is None:
        return 30
    minutes = seconds // 60
    if minutes <= 1:
        return 7
    if minutes <= 5:
        return 30
    if minutes <= 15:
        return 60
    return 120


class CandleAggregator:
    """Builds timeframe candles from ticks for one (symbol, exchange, tf).

    Confirms a candle only when a tick arrives in a LATER bucket (or the
    bucket is far in the past), so signals fire once per confirmed bar, never
    per tick. Shared by every strategy on the same instrument.
    """

    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe
        self.seconds = interval_to_seconds(timeframe)
        self.current: Bar | None = None
        self._last_volume = 0.0

    def seed(self, bar: Bar) -> None:
        """Seed from the last historical bar so live ticks continue it."""
        if self.current is None or bar.time >= self.current.time:
            self.current = Bar(
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                time=self._bucket_start(bar.time),
            )
            self._last_volume = bar.volume

    def _bucket_start(self, ts_ms: int) -> int:
        if self.seconds is None:
            # Daily+ timeframe: bucket = UTC day start (IST sessions make the
            # exact boundary cosmetic; confirmation is what matters).
            return ts_ms - (ts_ms % 86_400_000)
        bucket = self.seconds * 1000
        return ts_ms - (ts_ms % bucket)

    def on_tick(self, ltp: float, ltq: float, ts_ms: int) -> list[Bar]:
        """Feed one tick; returns the list of newly confirmed bars (0 or 1)."""
        confirmed: list[Bar] = []
        bucket = self._bucket_start(ts_ms)

        if self.current is not None and bucket > self.current.time:
            confirmed.append(self._confirm())
        elif self.current is not None and bucket < self.current.time:
            # Late tick for an already-passed bucket: drop it.
            return confirmed

        if self.current is None:
            self.current = Bar(
                open=ltp, high=ltp, low=ltp, close=ltp, volume=ltq, time=bucket
            )
        else:
            self.current.high = max(self.current.high, ltp)
            self.current.low = min(self.current.low, ltp)
            self.current.close = ltp
            self.current.volume += ltq
        return confirmed

    def confirm_if_stale(self, now_ms: int, grace_seconds: float = 90.0) -> list[Bar]:
        """Confirm the forming candle when the clock moved well past its close.

        Covers instruments whose next tick may take minutes (illiquid strikes)
        so a strategy still sees the bar confirmed shortly after it closes.
        """
        if self.current is None or self.seconds is None:
            return []
        bar_close_ms = self.current.time + self.seconds * 1000
        if now_ms - bar_close_ms >= grace_seconds * 1000:
            return [self._confirm()]
        return []

    def _confirm(self) -> Bar:
        bar = self.current
        self.current = None
        return bar


class StrategyRunner:
    """One running strategy instance: runtime + config + market binding."""

    def __init__(self, manager: "PineStrategyManager", instance, script, api_key: str) -> None:
        self.manager = manager
        self.instance_id = instance.id
        self.user_id = instance.user_id
        self.name = instance.name
        self.symbol = instance.symbol
        self.exchange = instance.exchange
        self.timeframe = instance.timeframe
        self.execution_mode = instance.execution_mode
        self.api_key = api_key
        self.paused = False
        self.last_bar_time: float = float(instance.last_bar_time or 0.0)
        self.lock = threading.Lock()

        self.script_ast = script  # compiled ast_nodes.Script
        inputs = json.loads(instance.inputs or "{}")
        self.runtime = PineRuntime(
            script,
            inputs=inputs,
            config=RuntimeConfig(
                default_qty=float(instance.quantity or 1),
                long_enabled=True,
                short_enabled=True,
            ),
        )
        self.quantity = int(instance.quantity or 1)
        self.product = instance.product or "MIS"

    # ------------------------------------------------------------------
    # Bar processing
    # ------------------------------------------------------------------

    def warmup(self, bars: list[Bar]) -> None:
        """Replay historical bars to rebuild indicator/strategy state.

        Historical signals are recorded (source=historical) for the chart and
        audit trail, but never routed to the order pipeline, and pending
        orders left over from the last bar are discarded.
        """
        for bar in bars:
            signals = self.runtime.process_bar(bar, realtime=False)
            for signal in signals:
                self._persist_signal(signal, source="historical", execute=False)
            for alert in self.runtime.alerts:
                if alert.bar_index == bar.index:
                    self._persist_alert(alert, source="historical")
            if bar.time > self.last_bar_time:
                self.last_bar_time = bar.time
        self.runtime.discard_pending()
        self._clear_runtime_alerts()

    def _clear_runtime_alerts(self) -> None:
        self.runtime.alerts.clear()

    def on_confirmed_bar(self, bar: Bar) -> None:
        """Process one confirmed realtime candle (executor thread)."""
        with self.lock:
            try:
                if bar.time <= self.last_bar_time:
                    # Already processed: pause windows, replays, reconnect
                    # backfills and server restarts all land here.
                    return

                signals = self.runtime.process_bar(bar, realtime=True)
                self.last_bar_time = bar.time

                new_alerts = [
                    a for a in self.runtime.alerts if a.bar_time == bar.time
                ]
                for alert in new_alerts:
                    self._persist_alert(alert, source="realtime")

                if self.paused:
                    # State advances so indicators stay continuous; signals
                    # are suppressed while paused (documented behaviour).
                    return

                for signal in signals:
                    recorded = self._persist_signal(signal, source="realtime", execute=True)
                    if recorded is None:
                        logger.info(
                            "Pine signal %s on %s %s was already processed (idempotent skip)",
                            signal.signal,
                            self.symbol,
                            self.timeframe,
                        )
                        continue
                    self._publish_signal_event(signal)
                    self._route_to_execution(signal)

                self.manager._touch_instance(self.instance_id, last_bar_time=bar.time)
            except PineError as error:
                self._handle_runtime_error(error)
            except Exception:
                logger.exception("Pine strategy %s failed on bar %s", self.name, bar.time)
                self._handle_runtime_error(None)

    # ------------------------------------------------------------------
    # Persistence + events
    # ------------------------------------------------------------------

    def _signal_id(self, signal, sequence: int) -> tuple[str, str]:
        """Deterministic signal id + idempotency key.

        Key = strategy + symbol + timeframe + bar_time + signal + sequence,
        exactly the duplicate-order protection the spec mandates: replays,
        reconnects, restarts and refreshes all reproduce the same key and are
        rejected by the unique index instead of re-ordering.
        """
        bar_time = int(signal.bar_time)
        idempotency_key = (
            f"{self.instance_id}|{self.symbol}|{self.timeframe}|{bar_time}|"
            f"{signal.signal}|{signal.order_id}|{sequence}"
        )
        signal_id = uuid.uuid5(uuid.NAMESPACE_URL, f"openalgo-pine:{idempotency_key}").hex
        return signal_id, idempotency_key

    def _persist_signal(self, signal, source: str, execute: bool):
        from database import pine_db

        for sequence in range(1, 64):
            signal_id, idempotency_key = self._signal_id(signal, sequence)
            if not pine_db.signal_key_exists(idempotency_key):
                break
        else:  # pragma: no cover - 64 identical signals on one bar
            return None

        return pine_db.record_signal(
            signal_id=signal_id,
            instance_id=self.instance_id,
            idempotency_key=idempotency_key,
            signal=signal.signal,
            kind=signal.kind,
            order_ref=signal.order_id,
            symbol=self.symbol,
            exchange=self.exchange,
            timeframe=self.timeframe,
            price=float(signal.price),
            quantity=float(signal.qty),
            bar_time=float(signal.bar_time),
            bar_index=int(signal.bar_index),
            source=source,
            executed=False,
        )

    def _persist_alert(self, alert, source: str) -> None:
        from database import pine_db

        stored = pine_db.record_alert(
            instance_id=self.instance_id,
            kind=alert.kind,
            title=alert.title,
            message=alert.message,
            symbol=self.symbol,
            exchange=self.exchange,
            timeframe=self.timeframe,
            bar_time=float(alert.bar_time),
        )
        if stored is not None and source == "realtime":
            from events.pine_events import PineAlertEvent

            bus.publish(
                PineAlertEvent(
                    mode=self.execution_mode.lower(),
                    strategy_id=self.instance_id,
                    strategy_name=self.name,
                    symbol=self.symbol,
                    exchange=self.exchange,
                    timeframe=self.timeframe,
                    alert_kind=alert.kind,
                    title=alert.title,
                    message=alert.message,
                    bar_time=float(alert.bar_time),
                )
            )

    def _publish_signal_event(self, signal) -> None:
        from events.pine_events import PineSignalEvent

        bus.publish(
            PineSignalEvent(
                mode=self.execution_mode.lower(),
                strategy_id=self.instance_id,
                strategy_name=self.name,
                symbol=self.symbol,
                exchange=self.exchange,
                timeframe=self.timeframe,
                signal=signal.signal,
                kind=signal.kind,
                price=float(signal.price),
                quantity=float(signal.qty),
                bar_time=float(signal.bar_time),
                bar_index=int(signal.bar_index),
                source="realtime",
            )
        )

    def _handle_runtime_error(self, error: PineError | None) -> None:
        message = error.message if error else "Unexpected runtime failure"
        logger.error(f"Pine strategy {self.name} runtime error: {message}")
        self.manager._touch_instance(
            self.instance_id, status="ERROR", last_error=message
        )
        from events.pine_events import PineErrorEvent

        bus.publish(
            PineErrorEvent(
                mode=self.execution_mode.lower(),
                strategy_id=self.instance_id,
                strategy_name=self.name,
                symbol=self.symbol,
                exchange=self.exchange,
                timeframe=self.timeframe,
                error=message,
            )
        )
        self.paused = True  # halt signal flow; operator resumes after fixing

    # ------------------------------------------------------------------
    # Execution routing
    # ------------------------------------------------------------------

    def _route_to_execution(self, signal) -> None:
        """Turn one signal intent into an order through the existing pipeline.

        PAPER: the existing sandbox engine (never a broker).
        LIVE: the existing place_order service (validation, action-center
        routing, broker adapter, event bus, logging).

        The Pine runtime never touches a broker API itself.
        """
        order_data = {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "action": signal.signal,
            "quantity": max(1, int(round(signal.qty or self.quantity))),
            "pricetype": "MARKET",
            "product": self.product,
            "price": "0",
            "trigger_price": "0",
            "disclosed_quantity": "0",
            "strategy": self.name,
        }

        try:
            if self.execution_mode == "LIVE":
                from services.place_order_service import place_order

                success, response, status_code = place_order(
                    order_data=order_data, api_key=self.api_key, emit_event=True
                )
            else:
                from services.sandbox_service import sandbox_place_order

                original = dict(order_data)
                original["apikey"] = self.api_key
                paper_order = dict(order_data)
                paper_order["quantity"] = int(paper_order["quantity"])
                success, response, status_code = sandbox_place_order(
                    paper_order, self.api_key, original
                )
                if success:
                    self._publish_paper_order_event(order_data, response)

            order_id = str(response.get("orderid", "")) if isinstance(response, dict) else ""
            status = "SUBMITTED" if success else "FAILED"
            self._mark_signal(signal, order_id, status)
            self._publish_order_event(signal, order_id, status, response)
        except Exception:
            logger.exception(
                "Failed to route Pine signal %s for %s", signal.signal, self.name
            )
            self._mark_signal(signal, "", "FAILED")
            self._publish_order_event(signal, "", "FAILED", {"message": "routing exception"})

    def _publish_paper_order_event(self, order_data: dict, response: dict) -> None:
        """Mirror place_order_service's analyze-mode OrderPlacedEvent so the
        sandbox order shows up in analyzer logs / socketio like any other."""
        from events.order_events import OrderPlacedEvent

        bus.publish(
            OrderPlacedEvent(
                mode="analyze",
                api_type="placeorder",
                strategy=order_data.get("strategy", ""),
                symbol=order_data.get("symbol", ""),
                exchange=order_data.get("exchange", ""),
                action=order_data.get("action", ""),
                quantity=int(order_data.get("quantity", 0)),
                pricetype=order_data.get("pricetype", ""),
                product=order_data.get("product", ""),
                orderid=response.get("orderid", "") if isinstance(response, dict) else "",
                request_data={k: v for k, v in order_data.items() if k != "apikey"},
                response_data=response if isinstance(response, dict) else {},
                api_key=self.api_key,
            )
        )

    def _mark_signal(self, signal, order_id: str, status: str) -> None:
        from database import pine_db

        # The signal row was just inserted by this thread; find it by key.
        for sequence in range(1, 64):
            signal_id, idempotency_key = self._signal_id(signal, sequence)
            if pine_db.signal_key_exists(idempotency_key):
                pine_db.mark_signal_executed(signal_id, order_id, status)
                return

    def _publish_order_event(self, signal, order_id: str, status: str, response) -> None:
        from events.pine_events import PineOrderEvent

        bus.publish(
            PineOrderEvent(
                mode=self.execution_mode.lower(),
                strategy_id=self.instance_id,
                strategy_name=self.name,
                symbol=self.symbol,
                exchange=self.exchange,
                timeframe=self.timeframe,
                signal_id="",
                signal=signal.signal,
                order_id=order_id,
                order_status=status,
                message=str(response.get("message", "")) if isinstance(response, dict) else "",
                response_data=response if isinstance(response, dict) else {},
            )
        )


class PineFeedDispatcher:
    """One market-data client per user, shared by that user's strategies.

    Subscribes through the existing WebSocket proxy (which pools broker
    adapters), so N strategies still mean ONE broker feed. Candle aggregators
    are keyed by (symbol, exchange, timeframe) and fan out confirmed bars to
    every runner watching that key.
    """

    def __init__(self, user_id: str, api_key: str) -> None:
        self.user_id = user_id
        self.api_key = api_key
        self.client = None
        self.aggregators: dict[tuple, CandleAggregator] = {}
        self.runners_by_key: dict[tuple, set[str]] = {}
        self.lock = threading.Lock()

    def start(self) -> bool:
        from services.websocket_client import WebSocketClient

        if self.client is not None and self.client.connected:
            return True
        host = "127.0.0.1"
        port = 8765
        import os

        port = int(os.getenv("WEBSOCKET_PORT", "8765"))
        client = WebSocketClient(self.api_key, host=host, port=port)
        if not client.connect():
            logger.error(f"Pine feed for user {self.user_id} could not connect to the proxy")
            return False
        client.register_callback("market_data", self._on_market_data)
        self.client = client
        return True

    def subscribe(self, symbol: str, exchange: str, timeframe: str) -> bool:
        if self.client is None and not self.start():
            return False
        key = (symbol, exchange, timeframe)
        with self.lock:
            if key not in self.aggregators:
                self.aggregators[key] = CandleAggregator(timeframe)
        if key in self._subscribed_keys():
            return True
        result = self.client.subscribe(
            [{"symbol": symbol, "exchange": exchange}], mode="Quote"
        )
        status = result.get("status")
        if status not in ("success", "partial"):
            logger.warning(
                "Pine subscribe failed for %s %s: %s", exchange, symbol, result.get("message")
            )
            return False
        return True

    def _subscribed_keys(self) -> set[tuple]:
        if self.client is None:
            return set()
        keys: set[tuple] = set()
        with self.client.lock:
            for sub_key in self.client.active_subscriptions:
                exchange, _, symbol = sub_key.partition(":")
                for (s, e, tf) in self.aggregators:
                    if s == symbol and e == exchange:
                        keys.add((s, e, tf))
        return keys

    def register_runner(self, runner: StrategyRunner) -> None:
        key = (runner.symbol, runner.exchange, runner.timeframe)
        with self.lock:
            self.runners_by_key.setdefault(key, set()).add(runner.instance_id)

    def unregister_runner(self, runner: StrategyRunner) -> None:
        key = (runner.symbol, runner.exchange, runner.timeframe)
        with self.lock:
            runners = self.runners_by_key.get(key)
            if runners:
                runners.discard(runner.instance_id)
                if not runners:
                    self.runners_by_key.pop(key, None)
                    self.aggregators.pop(key, None)
        # Unsubscribe at the proxy when this was the last consumer.
        if self.client is not None and key not in self.runners_by_key:
            try:
                self.client.unsubscribe(
                    [{"symbol": runner.symbol, "exchange": runner.exchange}], mode="Quote"
                )
            except Exception:
                logger.exception(
                    "Pine unsubscribe failed for %s %s", runner.exchange, runner.symbol
                )

    def seed_aggregator(self, symbol: str, exchange: str, timeframe: str, bar: Bar) -> None:
        key = (symbol, exchange, timeframe)
        with self.lock:
            aggregator = self.aggregators.get(key)
        if aggregator is not None:
            aggregator.seed(bar)

    def _on_market_data(self, message: dict) -> None:
        """Tick callback from the WebSocketClient thread."""
        try:
            data = message.get("data") or {}
            ltp = data.get("ltp")
            if ltp is None:
                return
            symbol = message.get("symbol")
            exchange = message.get("exchange")
            ltq = float(data.get("ltq") or 0)
            ts_ms = int(data.get("timestamp") or data.get("time") or time.time() * 1000)

            with self.lock:
                matching = [
                    (key, agg)
                    for key, agg in self.aggregators.items()
                    if key[0] == symbol and key[1] == exchange
                ]

            for (symbol_, exchange_, timeframe_), aggregator in matching:
                confirmed = aggregator.on_tick(float(ltp), ltq, ts_ms)
                for bar in confirmed:
                    self._dispatch(symbol_, exchange_, timeframe_, bar)
        except Exception:
            logger.exception("Pine feed tick handling failed")

    def _dispatch(self, symbol: str, exchange: str, timeframe: str, bar: Bar) -> None:
        with self.lock:
            instance_ids = list(self.runners_by_key.get((symbol, exchange, timeframe), set()))
        for instance_id in instance_ids:
            runner = manager.get_runner(instance_id)
            if runner is None:
                continue
            _executor.submit(runner.on_confirmed_bar, bar)

    def shutdown(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Pine feed disconnect failed")
            self.client = None
        with self.lock:
            self.aggregators.clear()
            self.runners_by_key.clear()


class PineStrategyManager:
    """Singleton owning every running Pine strategy in this process.

    Flask-SocketIO state is in-process and production is a single gunicorn
    worker, so an in-process registry is the correct home for live runners;
    the database remains the source of truth across restarts.
    """

    def __init__(self) -> None:
        self._runners: dict[str, StrategyRunner] = {}
        self._feeds: dict[str, PineFeedDispatcher] = {}
        self._lock = threading.RLock()
        self._watchdog_started = False

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_runner(self, instance_id: str) -> StrategyRunner | None:
        with self._lock:
            return self._runners.get(instance_id)

    def list_runners(self, user_id: str | None = None) -> list[StrategyRunner]:
        with self._lock:
            runners = list(self._runners.values())
        if user_id:
            runners = [r for r in runners if r.user_id == user_id]
        return runners

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, instance, script_code: str, api_key: str) -> tuple[bool, str]:
        """Compile, warm up on history, subscribe and go RUNNING."""
        if instance.id in self._runners:
            return True, "already running"

        result = compile_script(script_code)
        if not result.ok:
            self._touch_instance(instance.id, status="ERROR", last_error=result.error.message)
            return False, result.error.message

        bars = self._load_history(instance, api_key)
        if bars is None:
            message = "No historical candles available for warmup"
            self._touch_instance(instance.id, status="ERROR", last_error=message)
            return False, message

        runner = StrategyRunner(self, instance, result.script, api_key)
        runner.warmup(bars)

        with self._lock:
            feed = self._feeds.get(instance.user_id)
            if feed is None:
                feed = PineFeedDispatcher(instance.user_id, api_key)
                self._feeds[instance.user_id] = feed

        if not feed.start():
            message = "Could not connect to the market-data proxy"
            self._touch_instance(instance.id, status="ERROR", last_error=message)
            return False, message

        if not feed.subscribe(instance.symbol, instance.exchange, instance.timeframe):
            message = f"Subscribe failed for {instance.exchange}:{instance.symbol}"
            self._touch_instance(instance.id, status="ERROR", last_error=message)
            return False, message

        feed.register_runner(runner)
        if bars:
            feed.seed_aggregator(instance.symbol, instance.exchange, instance.timeframe, bars[-1])

        with self._lock:
            self._runners[instance.id] = runner
        self._touch_instance(
            instance.id, status="RUNNING", last_error=None, started_at=datetime.utcnow()
        )
        self._publish_status(instance, "RUNNING", "strategy started")
        self._ensure_watchdog()
        return True, "running"

    def pause(self, instance) -> tuple[bool, str]:
        runner = self.get_runner(instance.id)
        if runner is None:
            return False, "not running"
        runner.paused = True
        self._touch_instance(instance.id, status="PAUSED")
        self._publish_status(instance, "PAUSED", "strategy paused")
        return True, "paused"

    def resume(self, instance) -> tuple[bool, str]:
        runner = self.get_runner(instance.id)
        if runner is None:
            return False, "not running"
        runner.paused = False
        self._touch_instance(instance.id, status="RUNNING", last_error=None)
        self._publish_status(instance, "RUNNING", "strategy resumed")
        return True, "running"

    def stop(self, instance) -> tuple[bool, str]:
        runner = self.get_runner(instance.id)
        if runner is None:
            self._touch_instance(instance.id, status="STOPPED")
            return True, "not running"
        with self._lock:
            self._runners.pop(instance.id, None)
            feed = self._feeds.get(instance.user_id)
        if feed is not None:
            feed.unregister_runner(runner)
            if not feed.runners_by_key:
                feed.shutdown()
                with self._lock:
                    self._feeds.pop(instance.user_id, None)
        self._touch_instance(instance.id, status="STOPPED")
        self._publish_status(instance, "STOPPED", "strategy stopped")
        return True, "stopped"

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _load_history(self, instance, api_key: str) -> list[Bar] | None:
        """Fetch historical candles through the existing history service."""
        from services.history_service import get_history

        end = datetime.utcnow()
        start = end - timedelta(days=lookback_days(instance.timeframe))
        try:
            success, response, _status = get_history(
                symbol=instance.symbol,
                exchange=instance.exchange,
                interval=instance.timeframe,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
                api_key=api_key,
            )
        except Exception:
            logger.exception(
                "Pine warmup history fetch failed for %s %s",
                instance.exchange,
                instance.symbol,
            )
            return None
        if not success or not isinstance(response, dict):
            return None
        records = response.get("data")
        if not isinstance(records, list) or not records:
            return None

        bars: list[Bar] = []
        for record in records:
            try:
                ts = record.get("timestamp")
                if ts is None:
                    continue
                ts = float(ts)
                # Brokers differ on seconds vs milliseconds.
                if ts < 1e12:
                    ts *= 1000.0
                ts = int(ts)
                close = record.get("close")
                if close is None:
                    continue
                bars.append(
                    Bar(
                        open=float(record.get("open") or close),
                        high=float(record.get("high") or close),
                        low=float(record.get("low") or close),
                        close=float(close),
                        volume=float(record.get("volume") or 0),
                        time=ts,
                    )
                )
            except (TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b.time)
        return bars or None

    # ------------------------------------------------------------------
    # Status plumbing
    # ------------------------------------------------------------------

    def _touch_instance(self, instance_id: str, **kwargs) -> None:
        from database import pine_db

        pine_db.update_instance(instance_id, **kwargs)

    def _publish_status(self, instance, status: str, detail: str) -> None:
        from events.pine_events import PineStatusEvent

        bus.publish(
            PineStatusEvent(
                mode=(instance.execution_mode or "PAPER").lower(),
                strategy_id=instance.id,
                strategy_name=instance.name,
                symbol=instance.symbol,
                exchange=instance.exchange,
                timeframe=instance.timeframe,
                status=status,
                detail=detail,
            )
        )

    # ------------------------------------------------------------------
    # Watchdog: re-subscribe after proxy reconnects
    # ------------------------------------------------------------------

    def _ensure_watchdog(self) -> None:
        if self._watchdog_started:
            return
        self._watchdog_started = True
        thread = threading.Thread(target=self._watchdog_loop, name="pine-watchdog", daemon=True)
        thread.start()

    def _watchdog_loop(self) -> None:
        """Re-subscribe feed symbols after the WebSocketClient reconnected.

        The client reconnects on its own but subscriptions do not survive a
        reconnect; without this a silent proxy restart would leave strategies
        RUNNING but starved of ticks.
        """
        while True:
            time.sleep(30)
            try:
                with self._lock:
                    feeds = list(self._feeds.values())
                for feed in feeds:
                    if feed.client is None or not feed.client.connected:
                        if feed.client is not None and feed.client.running:
                            # reconnect in progress; subscriptions are
                            # restored below once connected again
                            pass
                        continue
                    with feed.lock:
                        keys = list(feed.aggregators.keys())
                    subscribed = feed._subscribed_keys()
                    for key in keys:
                        if key in subscribed:
                            continue
                        symbol, exchange, _tf = key
                        logger.info(
                            "Pine watchdog resubscribing %s:%s after reconnect",
                            exchange,
                            symbol,
                        )
                        feed.client.subscribe(
                            [{"symbol": symbol, "exchange": exchange}], mode="Quote"
                        )
                    # Confirm stale forming candles so strategies keep moving
                    # when the instrument goes quiet.
                    now_ms = int(time.time() * 1000)
                    with feed.lock:
                        aggregators = list(feed.aggregators.items())
                    for key, aggregator in aggregators:
                        for bar in aggregator.confirm_if_stale(now_ms):
                            feed._dispatch(key[0], key[1], key[2], bar)
            except Exception:
                logger.exception("Pine watchdog iteration failed")

    def shutdown(self) -> None:
        """Stop every feed; used on process exit."""
        with self._lock:
            feeds = list(self._feeds.values())
            self._feeds.clear()
            self._runners.clear()
        for feed in feeds:
            feed.shutdown()


# Module-level singleton (single gunicorn worker keeps this authoritative).
manager = PineStrategyManager()


# ---------------------------------------------------------------------------
# Boot recovery
# ---------------------------------------------------------------------------


def restore_pine_strategies() -> int:
    """Resume strategy instances that were active before a restart.

    Each instance is restarted through the normal start() path: fresh
    historical warmup, feed subscription, then realtime bars. The bar-time
    guard in StrategyRunner.on_confirmed_bar plus the signal idempotency key
    make the recovery itself duplicate-proof.
    """
    from database import pine_db
    from database.auth_db import get_api_key_for_tradingview

    restored = 0
    instances = pine_db.get_active_instances()
    if not instances:
        return 0

    logger.info(f"Restoring {len(instances)} active Pine strategy instance(s)")
    for instance in instances:
        try:
            script = pine_db.get_script(instance.script_id)
            if script is None:
                pine_db.update_instance(instance.id, status="ERROR", last_error="script missing")
                continue
            api_key = get_api_key_for_tradingview(instance.user_id)
            if not api_key:
                pine_db.update_instance(
                    instance.id, status="ERROR", last_error="no api key for user"
                )
                continue
            ok, _ = manager.start(instance, script.code, api_key)
            if ok:
                restored += 1
        except Exception:
            logger.exception("Failed to restore Pine strategy %s", instance.id)
            pine_db.update_instance(instance.id, status="ERROR", last_error="restore failed")
    return restored


# ---------------------------------------------------------------------------
# One-shot evaluation / backtest (chart + backtest endpoints)
# ---------------------------------------------------------------------------


def evaluate_script(
    code: str,
    symbol: str,
    exchange: str,
    timeframe: str,
    api_key: str,
    inputs: dict | None = None,
) -> tuple[bool, dict]:
    """Compile + run a script over history and return chart-ready output."""
    result = compile_script(code)
    if not result.ok:
        return False, {"error": result.error.to_dict()}

    # SimpleNamespace, not a class body: `symbol = symbol` inside a class body
    # resolves against the not-yet-bound class attribute and raises NameError,
    # which the app-wide 500 handler turns into a redirect - the browser then
    # shows a generic "Evaluation failed" with nothing in the console.
    instance = SimpleNamespace(
        id="evaluate",
        user_id="evaluate",
        name=result.title,
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
    )
    bars = manager._load_history(instance, api_key)
    if bars is None:
        return False, {"error": {"type": "compile_error", "line": 0, "column": 0,
                                 "message": "No historical data available for this symbol/exchange/timeframe"}}

    runtime = PineRuntime(result.script, inputs=inputs or {})
    for bar in bars:
        runtime.process_bar(bar)
    output = _runtime_payload(runtime, bars)
    output["meta"] = {
        "title": result.title,
        "kind": result.kind,
        "overlay": result.overlay,
    }
    output["inputs"] = result.inputs
    return True, output


def backtest_script(
    code: str,
    symbol: str,
    exchange: str,
    timeframe: str,
    api_key: str,
    inputs: dict | None = None,
    config: dict | None = None,
) -> tuple[bool, dict]:
    """Run a backtest with the same runtime used for live execution."""
    result = compile_script(code)
    if not result.ok:
        return False, {"error": result.error.to_dict()}

    # Same SimpleNamespace fix as evaluate_script: a class body assigning
    # `symbol = symbol` reads its own (unbound) attribute and raises NameError.
    instance = SimpleNamespace(
        id="backtest",
        user_id="backtest",
        name=result.title,
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
    )
    bars = manager._load_history(instance, api_key)
    if bars is None:
        return False, {"error": {"type": "compile_error", "line": 0, "column": 0,
                                 "message": "No historical data available for this symbol/exchange/timeframe"}}

    runtime_config = RuntimeConfig(
        initial_capital=float((config or {}).get("initial_capital", 100000)),
        commission_pct=float((config or {}).get("commission_pct", 0)),
        slippage_ticks=float((config or {}).get("slippage_ticks", 0)),
        tick_size=float((config or {}).get("tick_size", 0.05)),
        long_enabled=bool((config or {}).get("long_enabled", True)),
        short_enabled=bool((config or {}).get("short_enabled", True)),
    )
    runtime_result, metrics = run_backtest(bars, result.script, inputs=inputs or {}, config=runtime_config)
    output = _runtime_payload(runtime_result, bars)
    output["meta"] = {"title": result.title, "kind": result.kind, "overlay": result.overlay}
    output["metrics"] = {
        "total_trades": metrics.total_trades,
        "winning_trades": metrics.winning_trades,
        "losing_trades": metrics.losing_trades,
        "win_rate": round(metrics.win_rate, 2),
        "gross_profit": round(metrics.gross_profit, 2),
        "gross_loss": round(metrics.gross_loss, 2),
        "net_profit": round(metrics.net_profit, 2),
        "max_drawdown": round(metrics.max_drawdown, 2),
        "profit_factor": (
            round(metrics.profit_factor, 3) if math.isfinite(metrics.profit_factor) else None
        ),
        "initial_capital": metrics.initial_capital,
        "final_equity": round(metrics.final_equity, 2),
        "return_pct": round(metrics.return_pct, 3),
        "equity_curve": metrics.equity_curve,
        "trade_list": metrics.trade_list,
    }
    return True, output


def _runtime_payload(runtime, bars: list[Bar]) -> dict:
    """Chart-ready serialization of a runtime result.

    Accepts either a live ``PineRuntime`` (evaluate path, which keeps trades
    and position on the simulator) or the ``RuntimeResult`` snapshot the
    backtester returns (flat fields), so both render identically.
    """
    from pine.runtime import NA, RuntimeResult

    if isinstance(runtime, RuntimeResult):
        trades = runtime.trades
        position_size = runtime.position_size
        position_avg_price = runtime.position_avg_price
    else:
        trades = runtime.sim.trades
        position_size = runtime.sim.position
        position_avg_price = runtime.sim.avg_price

    def clean(value):
        return None if value is NA or value is None else value

    times = [bar.time for bar in bars]
    plots = []
    for plot in runtime.plots:
        plots.append(
            {
                "id": plot.id,
                "title": plot.title,
                "color": plot.color,
                "data": [
                    {"time": times[i], "value": clean(v)}
                    for i, v in enumerate(plot.values)
                    if i < len(times)
                ],
            }
        )
    shapes = [
        {
            "time": shape.time,
            "title": shape.title,
            "style": shape.style,
            "location": shape.location,
            "color": shape.color,
            "text": shape.text,
        }
        for shape in runtime.shapes
    ]
    hlines = [
        {"price": hline.price, "title": hline.title, "color": hline.color}
        for hline in runtime.hlines
    ]
    signals = [
        {
            "signal": signal.signal,
            "kind": signal.kind,
            "order_id": signal.order_id,
            "qty": signal.qty,
            "time": signal.bar_time,
            "price": signal.price,
        }
        for signal in runtime.signals
    ]
    trades = [
        {
            "entry_id": trade.entry_id,
            "direction": trade.direction,
            "qty": trade.qty,
            "entry_time": trade.entry_time,
            "entry_price": trade.entry_price,
            "exit_time": trade.exit_time,
            "exit_price": trade.exit_price,
            "pnl": round(trade.pnl, 2),
            "exit_reason": trade.exit_reason,
        }
        for trade in trades
    ]
    alerts = [
        {
            "kind": alert.kind,
            "title": alert.title,
            "message": alert.message,
            "time": alert.bar_time,
        }
        for alert in runtime.alerts
    ]
    return {
        "plots": plots,
        "shapes": shapes,
        "hlines": hlines,
        "signals": signals,
        "trades": trades,
        "alerts": alerts,
        "bars_processed": len(bars),
        "position_size": position_size,
        "position_avg_price": position_avg_price,
    }
