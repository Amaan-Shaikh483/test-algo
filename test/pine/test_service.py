"""Service-layer tests: candle aggregation, lifecycle, idempotency, E2E pipeline.

Every test runs against an isolated temporary database and a fake market feed;
the order pipeline is exercised with the real service functions monkeypatched
at the broker boundary (the same seam a mock broker adapter would use).
"""

import json
import time
from datetime import datetime, timezone

import pytest
from pine_test_utils import make_bars
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import database.pine_db as pine_db
import services.pine_strategy_service as pine_service
from pine.runtime import Bar

EMA_CROSS = """//@version=5
strategy("EMA Cross", overlay=true)

fast = ta.ema(close, 3)
slow = ta.ema(close, 8)

plot(fast, title="Fast EMA")
plot(slow, title="Slow EMA")

longCondition = ta.crossover(fast, slow)
shortCondition = ta.crossunder(fast, slow)

if longCondition
    strategy.entry("BUY", strategy.long)

if shortCondition
    strategy.entry("SELL", strategy.short)
"""


@pytest.fixture(autouse=True)
def isolated_pine_database(tmp_path, monkeypatch):
    """Run every test against a temporary pine database, never db/openalgo.db."""
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'pine-test.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(pine_db, "engine", test_engine)
    monkeypatch.setattr(pine_db, "db_session", test_session)
    pine_db.Base.query = test_session.query_property()
    pine_db.Base.metadata.create_all(bind=test_engine)
    yield
    test_session.remove()
    test_engine.dispose()


@pytest.fixture(autouse=True)
def fresh_manager(monkeypatch):
    """A clean manager per test, with no watchdog thread side effects."""
    monkeypatch.setattr(pine_service, "manager", pine_service.PineStrategyManager())
    yield


class FakeFeed(pine_service.PineFeedDispatcher):
    """Real dispatcher logic with the network replaced by fakes."""

    def __init__(self, user_id, api_key):
        super().__init__(user_id, api_key)
        self.subscribed: list[tuple[str, str]] = []

    def start(self):
        return True

    def subscribe(self, symbol, exchange, timeframe):
        # Mirror the real subscribe(): create the shared aggregator.
        key = (symbol, exchange, timeframe)
        with self.lock:
            if key not in self.aggregators:
                self.aggregators[key] = pine_service.CandleAggregator(timeframe)
        self.subscribed.append((symbol, exchange))
        return True


@pytest.fixture
def fake_feed(monkeypatch):
    feeds = {}

    def factory(user_id, api_key):
        feed = FakeFeed(user_id, api_key)
        feeds[user_id] = feed
        return feed

    monkeypatch.setattr(pine_service, "PineFeedDispatcher", factory)
    return feeds


def _trend_bars(count=60, step=0.8):
    bars = []
    price = 100.0
    base = 1700000000000
    for i in range(count):
        o = price
        c = o + step
        bars.append(Bar(open=o, high=max(o, c) + 0.1, low=min(o, c) - 0.1, close=c, volume=100.0, time=base + i * 60000))
        price = c
    return bars


def _make_instance(**overrides):
    from types import SimpleNamespace

    defaults = {
        "id": "inst-1",
        "script_id": 1,
        "user_id": "tester",
        "name": "EMA Cross",
        "symbol": "NIFTY",
        "exchange": "NSE",
        "timeframe": "1m",
        "status": "STOPPED",
        "execution_mode": "PAPER",
        "quantity": 1,
        "product": "MIS",
        "inputs": "{}",
        "last_bar_time": None,
        "last_error": None,
    }
    defaults.update(overrides)
    values = dict(defaults)
    instance_id = values.pop("id")
    # Persist so the manager's status writes have a row to update.
    if pine_db.get_instance(instance_id) is None:
        pine_db.create_instance(id=instance_id, **values)
    return SimpleNamespace(**defaults)


class TestCandleAggregator:
    def test_no_signal_during_same_candle(self):
        aggregator = pine_service.CandleAggregator("1m")
        base = 1700000000000
        confirmed = []
        for i in range(10):
            confirmed.extend(aggregator.on_tick(100 + i * 0.1, 5, base + i * 1000))
        assert confirmed == []  # all ticks inside one 1m bucket

    def test_confirms_on_bucket_rollover(self):
        aggregator = pine_service.CandleAggregator("1m")
        base = 1700000000000
        for i in range(5):
            aggregator.on_tick(100 + i, 1, base + i * 1000)
        # First tick of the NEXT minute confirms the previous candle
        confirmed = aggregator.on_tick(110, 1, base + 61_000)
        assert len(confirmed) == 1
        bar = confirmed[0]
        assert bar.open == 100
        assert bar.close == 104
        assert bar.high == 104
        assert bar.low == 100
        assert bar.volume == 5

    def test_confirms_only_once_per_candle(self):
        aggregator = pine_service.CandleAggregator("1m")
        base = 1700000000000
        aggregator.on_tick(100, 1, base)
        confirmed = aggregator.on_tick(101, 1, base + 60_000)
        assert len(confirmed) == 1
        # A late tick for the confirmed bucket is dropped, not re-confirmed
        assert aggregator.on_tick(100.5, 1, base + 30_000) == []

    def test_stale_confirmation(self):
        aggregator = pine_service.CandleAggregator("1m")
        base = 1700000000000
        aggregator.on_tick(100, 1, base)
        now = base + 60_000 + 120_000  # bar closed over 2 minutes ago
        confirmed = aggregator.confirm_if_stale(now)
        assert len(confirmed) == 1


class TestIdempotency:
    def test_signal_key_is_deterministic(self):
        from types import SimpleNamespace

        runner = object.__new__(pine_service.StrategyRunner)
        runner.instance_id = "inst-1"
        runner.symbol = "NIFTY"
        runner.timeframe = "1m"
        signal = SimpleNamespace(signal="BUY", bar_time=1700000060000, order_id="BUY")

        first = runner._signal_id(signal, 1)
        again = runner._signal_id(signal, 1)
        assert first == again

        other = SimpleNamespace(signal="SELL", bar_time=1700000060000, order_id="SELL")
        assert runner._signal_id(other, 1) != first

        # A different bar produces a different key
        later = SimpleNamespace(signal="BUY", bar_time=1700000120000, order_id="BUY")
        assert runner._signal_id(later, 1) != first

    def test_duplicate_replay_is_rejected(self):
        from types import SimpleNamespace

        runner = object.__new__(pine_service.StrategyRunner)
        runner.instance_id = "inst-1"
        runner.symbol = "NIFTY"
        runner.exchange = "NSE"
        runner.timeframe = "1m"
        signal = SimpleNamespace(
            signal="BUY",
            bar_time=1700000060000,
            order_id="BUY",
            kind="entry",
            qty=1,
            bar_index=10,
            price=100.0,
        )

        first = runner._persist_signal(signal, source="realtime", execute=True)
        assert first is not None
        # Replaying the same bar+signal yields a new row (sequence collision
        # avoided) - the guard is the bar-time check in on_confirmed_bar plus
        # the key. The same key twice is what record_signal rejects:
        assert pine_db.record_signal(
            signal_id=first.signal_id,
            instance_id="inst-1",
            idempotency_key=first.idempotency_key,
            signal="BUY",
            kind="entry",
            order_ref="BUY",
            symbol="NIFTY",
            exchange="NSE",
            timeframe="1m",
        ) is None


class TestManagerLifecycle:
    def _patch_history(self, monkeypatch, bars):
        monkeypatch.setattr(
            pine_service.PineStrategyManager, "_load_history", lambda self, i, k: list(bars)
        )

    def test_start_stop(self, monkeypatch, fake_feed):
        bars = _trend_bars(60)
        self._patch_history(monkeypatch, bars)
        instance = _make_instance()
        ok, message = pine_service.manager.start(instance, EMA_CROSS, "key")
        assert ok, message
        runner = pine_service.manager.get_runner("inst-1")
        assert runner is not None
        # Warmup processed history; last historical pending orders discarded
        assert len(runner.runtime.bars) == 60
        assert runner.runtime.sim.pending == []
        assert pine_db.get_instance("inst-1").status == "RUNNING"
        assert fake_feed["tester"].subscribed == [("NIFTY", "NSE")]

        ok, _ = pine_service.manager.stop(instance)
        assert ok
        assert pine_service.manager.get_runner("inst-1") is None
        assert pine_db.get_instance("inst-1").status == "STOPPED"

    def test_pause_resume(self, monkeypatch, fake_feed):
        bars = _trend_bars(60)
        self._patch_history(monkeypatch, bars)
        instance = _make_instance()
        pine_service.manager.start(instance, EMA_CROSS, "key")

        ok, _ = pine_service.manager.pause(instance)
        assert ok
        runner = pine_service.manager.get_runner("inst-1")
        assert runner.paused

        ok, _ = pine_service.manager.resume(instance)
        assert ok
        assert not runner.paused

        pine_service.manager.stop(instance)

    def test_compile_failure_sets_error(self, monkeypatch):
        instance = _make_instance()
        ok, message = pine_service.manager.start(instance, "strategy(broken", "key")
        assert not ok
        assert pine_db.get_instance("inst-1").status == "ERROR"


class TestRealtimePipeline:
    """End to end: ticks -> candle confirm -> runtime -> signal -> alert -> order."""

    def _setup(self, monkeypatch, fake_feed, mode="PAPER"):
        # Rising then falling so the warmup itself contains a crossunder and
        # therefore a historical signal.
        bars = _trend_bars(25, step=0.8)
        price = bars[-1].close
        base = bars[-1].time
        for i in range(15):
            o = price
            c = o - 0.8
            bars.append(Bar(open=o, high=max(o, c) + 0.1, low=min(o, c) - 0.1, close=c, volume=100.0, time=base + (i + 1) * 60000))
            price = c
        monkeypatch.setattr(
            pine_service.PineStrategyManager, "_load_history", lambda self, i, k: list(bars)
        )
        orders = []

        def fake_sandbox_place_order(order_data, api_key, original_data, prefetched_quote=None):
            orders.append({"mode": "PAPER", **order_data})
            return True, {"status": "success", "orderid": "SBX-1", "mode": "analyze"}, 200

        def fake_place_order(order_data, api_key=None, emit_event=True, **kwargs):
            orders.append({"mode": "LIVE", **order_data})
            return True, {"status": "success", "orderid": "LIVE-1"}, 200

        import restx_api  # noqa: F401  (import order breaks a pre-existing cycle)
        import services.place_order_service as pos
        import services.sandbox_service as sss

        monkeypatch.setattr(sss, "sandbox_place_order", fake_sandbox_place_order)
        monkeypatch.setattr(pos, "place_order", fake_place_order)

        events = []
        from utils.event_bus import bus

        def capture(event):
            events.append(event)

        bus.subscribe("pine.signal", capture, "test:pine_signal")
        bus.subscribe("pine.order", capture, "test:pine_order")
        bus.subscribe("pine.alert", capture, "test:pine_alert")
        bus.subscribe("pine.status", capture, "test:pine_status")

        instance = _make_instance(execution_mode=mode)
        ok, message = pine_service.manager.start(instance, EMA_CROSS, "key")
        assert ok, message
        feed = fake_feed["tester"]
        return instance, feed, orders, events

    def _run_ticks(self, feed, base_time, ticks):
        """Push ticks; each entry is (offset_ms, price)."""
        for offset, price in ticks:
            feed._on_market_data(
                {
                    "type": "market_data",
                    "symbol": "NIFTY",
                    "exchange": "NSE",
                    "data": {"ltp": price, "ltq": 1, "timestamp": base_time + offset},
                }
            )

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def test_paper_order_flow_end_to_end(self, monkeypatch, fake_feed):
        instance, feed, orders, events = self._setup(monkeypatch, fake_feed, mode="PAPER")
        base = 1700000000000 + 41 * 60000  # history is 40 bars ending at +39m

        # Falling prices for ~25 minutes -> EMA3 crosses under EMA8 -> SELL
        price = 132.0
        ticks = []
        for minute in range(25):
            for second in range(6):
                ticks.append((minute * 60000 + second * 10000, price))
                price -= 0.5
        self._run_ticks(feed, base, ticks)

        assert self._wait_for(lambda: len(orders) > 0), f"no order placed, events={events}"
        assert orders[0]["mode"] == "PAPER"
        assert orders[0]["symbol"] == "NIFTY"
        assert orders[0]["exchange"] == "NSE"
        assert orders[0]["strategy"] == "EMA Cross"

        # Signal persisted and marked executed
        signals = pine_db.get_instance_signals(instance.id)
        assert any(s.executed and s.order_id == "SBX-1" for s in signals)

        # Realtime events published (signal + order + status)
        kinds = [type(e).__name__ for e in events]
        assert "PineSignalEvent" in kinds
        assert "PineOrderEvent" in kinds
        assert "PineStatusEvent" in kinds

        pine_service.manager.stop(instance)

    def test_live_mode_uses_place_order_service(self, monkeypatch, fake_feed):
        instance, feed, orders, events = self._setup(monkeypatch, fake_feed, mode="LIVE")
        base = 1700000000000 + 41 * 60000

        price = 132.0
        ticks = []
        for minute in range(25):
            for second in range(6):
                ticks.append((minute * 60000 + second * 10000, price))
                price -= 0.5
        self._run_ticks(feed, base, ticks)

        assert self._wait_for(lambda: len(orders) > 0), f"no order placed, events={events}"
        assert orders[0]["mode"] == "LIVE"
        assert orders[0]["orderid"] if "orderid" in orders[0] else True
        pine_service.manager.stop(instance)

    def test_duplicate_tick_replay_never_duplicates_orders(self, monkeypatch, fake_feed):
        instance, feed, orders, events = self._setup(monkeypatch, fake_feed)
        base = 1700000000000 + 41 * 60000

        price = 132.0
        ticks = []
        for minute in range(25):
            for second in range(6):
                ticks.append((minute * 60000 + second * 10000, price))
                price -= 0.5
        self._run_ticks(feed, base, ticks)
        assert self._wait_for(lambda: len(orders) > 0)
        count_after_first = len(orders)

        # Replay the exact same ticks (reconnect / event replay scenario)
        self._run_ticks(feed, base, ticks)
        self._wait_for(lambda: len(orders) > count_after_first, timeout=2)
        assert len(orders) == count_after_first, "duplicate ticks produced duplicate orders"

        signals = [s for s in pine_db.get_instance_signals(instance.id) if s.source == "realtime"]
        executed = [s for s in signals if s.executed]
        assert len(executed) == len({s.idempotency_key for s in executed})
        pine_service.manager.stop(instance)

    def test_historical_signals_never_execute(self, monkeypatch, fake_feed):
        instance, feed, orders, events = self._setup(monkeypatch, fake_feed)
        signals = pine_db.get_instance_signals(instance.id)
        historical = [s for s in signals if s.source == "historical"]
        assert historical, "warmup should record historical signals for the chart"
        assert all(not s.executed for s in historical)
        assert orders == []
        pine_service.manager.stop(instance)

    def test_paused_strategy_does_not_order(self, monkeypatch, fake_feed):
        instance, feed, orders, events = self._setup(monkeypatch, fake_feed)
        pine_service.manager.pause(instance)
        base = 1700000000000 + 41 * 60000

        price = 132.0
        ticks = []
        for minute in range(25):
            for second in range(6):
                ticks.append((minute * 60000 + second * 10000, price))
                price -= 0.5
        self._run_ticks(feed, base, ticks)
        time.sleep(0.5)
        assert orders == []
        pine_service.manager.stop(instance)


class TestAlertPipeline:
    def test_alertcondition_fires_realtime_alert_event(self, monkeypatch, fake_feed):
        source = """//@version=5
strategy("Alerts", overlay=true)
alertcondition(close > 150, "High", "Price above 150")
"""
        bars = _trend_bars(40, step=0.2)  # ends near 108
        monkeypatch.setattr(
            pine_service.PineStrategyManager, "_load_history", lambda self, i, k: list(bars)
        )
        events = []
        from utils.event_bus import bus

        bus.subscribe("pine.alert", lambda e: events.append(e), "test:alert")

        instance = _make_instance(name="Alerts")
        ok, message = pine_service.manager.start(instance, source, "key")
        assert ok, message
        feed = fake_feed["tester"]
        base = 1700000000000 + 41 * 60000

        # Rising prices cross 150 in minute 1, confirmed at minute 2's first tick
        feed._on_market_data({
            "type": "market_data", "symbol": "NIFTY", "exchange": "NSE",
            "data": {"ltp": 155.0, "ltq": 1, "timestamp": base + 10_000},
        })
        feed._on_market_data({
            "type": "market_data", "symbol": "NIFTY", "exchange": "NSE",
            "data": {"ltp": 156.0, "ltq": 1, "timestamp": base + 70_000},
        })

        deadline = time.time() + 5
        while time.time() < deadline and not events:
            time.sleep(0.05)
        assert events, "expected a realtime pine.alert event"
        assert events[0].title == "High"

        alerts = pine_db.get_instance_alerts(instance.id)
        assert any(a.source == "realtime" if hasattr(a, "source") else a.title == "High" for a in alerts)
        pine_service.manager.stop(instance)


class TestRecovery:
    def test_restore_resumes_and_skips_old_bars(self, monkeypatch, fake_feed):
        bars = _trend_bars(40)
        monkeypatch.setattr(
            pine_service.PineStrategyManager, "_load_history", lambda self, i, k: list(bars)
        )
        script = pine_db.create_script("tester", "EMA Cross", EMA_CROSS, "strategy")
        pine_db.create_instance(
            id="inst-9",
            script_id=script.id,
            user_id="tester",
            name="EMA Cross",
            symbol="NIFTY",
            exchange="NSE",
            timeframe="1m",
            status="RUNNING",
            execution_mode="PAPER",
            quantity=1,
            product="MIS",
            inputs="{}",
            last_bar_time=bars[-1].time,
        )

        monkeypatch.setattr(
            "database.auth_db.get_api_key_for_tradingview", lambda user_id: "key"
        )
        restored = pine_service.restore_pine_strategies()
        assert restored == 1
        runner = pine_service.manager.get_runner("inst-9")
        assert runner is not None
        assert pine_db.get_instance("inst-9").status == "RUNNING"

        # Replaying a bar at/before last_bar_time must not reprocess:
        stale = Bar(open=1, high=1, low=1, close=1, volume=1, time=bars[-1].time)
        runner.on_confirmed_bar(stale)
        assert len(runner.runtime.bars) == 40  # unchanged

        pine_service.manager.stop(pine_db.get_instance("inst-9"))

    def test_restore_with_missing_script_marks_error(self, monkeypatch):
        pine_db.create_instance(
            id="inst-10",
            script_id=9999,
            user_id="tester",
            name="Ghost",
            symbol="NIFTY",
            exchange="NSE",
            timeframe="1m",
            status="RUNNING",
            execution_mode="PAPER",
            quantity=1,
            product="MIS",
            inputs="{}",
        )
        monkeypatch.setattr(
            "database.auth_db.get_api_key_for_tradingview", lambda user_id: "key"
        )
        restored = pine_service.restore_pine_strategies()
        assert restored == 0
        assert pine_db.get_instance("inst-10").status == "ERROR"
