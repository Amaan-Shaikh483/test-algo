"""Alert Engine — matches price/strategy conditions and delivers webhooks.

Design (mirrors the Pine strategy service, which is the reference pattern):

- Strategy-signal alerts subscribe to the existing ``pine.signal`` event-bus
  topic. The Pine runtime never calls a webhook; it only publishes events.
- Price alerts reuse the existing WebSocket proxy (one ``WebSocketClient`` per
  user, exactly like ``PineFeedDispatcher``); ticks are evaluated against
  active alerts. No new broker feed is created.
- Webhook delivery runs on a small ``ThreadPoolExecutor`` (eventlet-safe with
  ``requests``; no asyncio) with a bounded retry schedule and a deterministic
  idempotency key, so a retried POST is logically the same event.
- Nothing here ever places orders. ``test_webhook`` sends a ``"test"`` event.

Threading model:
- ``bus`` subscriber callbacks already run on the event-bus pool.
- WS tick callbacks run on the WebSocketClient thread.
- All engine state is guarded by one lock; DB writes use short sessions.
"""

import atexit
import ipaddress
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from database.alert_db import (
    Alert,
    AlertDelivery,
    AlertEvent,
    db_session,
)
from database.alert_db import (
    init_db as alert_init_db,
)
from events.alert_events import AlertDeliveryEvent, AlertTriggeredEvent
from utils.env_config import env_int
from utils.event_bus import bus
from utils.logging import get_logger

logger = get_logger(__name__)

# Deterministic namespace for idempotency keys: same alert + source + bar
# timestamp + signal must always produce the same key, across restarts too.
IDEMPOTENCY_NAMESPACE = uuid.UUID("8d5f3f6e-2c1b-4a7e-9d0a-a1b2c3d4e5f6")

#: Bounded delivery pool: a slow webhook must never spawn unbounded threads or
#: block market-data processing. Mirrors the flow price-alert pool.
_DELIVERY_POOL = ThreadPoolExecutor(
    max_workers=env_int("ALERT_WEBHOOK_WORKERS", 4, minimum=1),
    thread_name_prefix="alert-webhook",
)

#: Seconds to wait between delivery attempts (attempt 1 fires immediately).
#: Defaults cover timeout + transient errors; override for tests.
RETRY_DELAYS = tuple(
    int(x) for x in os.environ.get("ALERT_WEBHOOK_RETRY_DELAYS", "0,10,30").split(",") if x.strip()
) or (0,)

#: Webhook HTTP timeout in seconds (spec: 5-10s).
WEBHOOK_TIMEOUT = max(1, min(30, env_int("ALERT_WEBHOOK_TIMEOUT", 8, minimum=1)))

#: Self-hosted installs may target internal services; cloud/SaaS must never
#: expose the metadata endpoint. Both are opt-in via env, never via API input.
ALLOW_PRIVATE_WEBHOOKS = os.getenv("ALLOW_PRIVATE_WEBHOOKS", "").strip().lower() in ("1", "true", "yes")

PRICE_OPERATORS = {
    "crossing",
    "crossing_up",
    "crossing_down",
    "greater_than",
    "less_than",
    "greater_than_equal",
    "less_than_equal",
}

STRATEGY_SIGNALS = {"BUY", "SELL", "ANY"}


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """SSRF guard for user-supplied webhook URLs.

    Returns (ok, reason). Blocks non-HTTP(S) schemes, missing/odd hostnames,
    localhost names, and private/loopback/link-local (cloud metadata) IPs
    unless ``ALLOW_PRIVATE_WEBHOOKS=true``.
    """
    if not url or not isinstance(url, str):
        return False, "Webhook URL is required"
    url = url.strip()
    if len(url) > 2048 or any(c.isspace() for c in url):
        return False, "Webhook URL must not contain whitespace"
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "Webhook URL is not a valid URL"
    if parsed.scheme not in ("http", "https"):
        return False, "Webhook URL must use http:// or https://"
    hostname = parsed.hostname
    if not hostname:
        return False, "Webhook URL has no hostname"
    if hostname.lower() in ("localhost",) or hostname.lower().endswith(".localhost"):
        if not ALLOW_PRIVATE_WEBHOOKS:
            return False, "Webhook URL must not point at localhost"
    # Literal IPs are checked directly; DNS names are resolved so a hostname
    # like internal.corp is caught at create time too.
    try:
        addr = ipaddress.ip_address(hostname)
        targets = [addr]
    except ValueError:
        targets = []
        if not ALLOW_PRIVATE_WEBHOOKS:
            import socket

            try:
                for info in socket.getaddrinfo(hostname, None):
                    targets.append(ipaddress.ip_address(info[4][0]))
            except (socket.gaierror, ValueError):
                return False, f"Webhook hostname '{hostname}' could not be resolved"
    if not ALLOW_PRIVATE_WEBHOOKS:
        for addr in targets:
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_unspecified
                or addr.is_reserved
            ):
                return False, f"Webhook URL must not point at a private or reserved address ({addr})"
    return True, "ok"


def make_idempotency_key(
    alert_id: str, source: str, bar_time: float, signal: str
) -> str:
    """Deterministic key: alert + source + bar timestamp + signal."""
    raw = f"{alert_id}|{source}|{int(bar_time)}|{signal}"
    return uuid.uuid5(IDEMPOTENCY_NAMESPACE, raw).hex


def _now() -> datetime:
    return datetime.now()


def _ist_iso(epoch_ms: float | None) -> str:
    """Format an epoch-ms value as an IST ISO-8601 string (repo convention)."""
    from pytz import timezone as tz

    ist = tz("Asia/Kolkata")
    if epoch_ms is None:
        epoch_ms = time.time() * 1000
    dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=ist)
    return dt.isoformat()


class AlertFeed:
    """One market-data client per user, shared by all their price alerts.

    Reuses the existing WebSocket proxy via ``services.websocket_client``
    (the same client ``PineFeedDispatcher`` uses), so N alerts across any
    number of symbols still mean ONE broker feed per user.
    """

    def __init__(self, user_id: str, api_key: str, on_tick) -> None:
        self.user_id = user_id
        self.api_key = api_key
        self.on_tick = on_tick
        self.client = None
        self.lock = threading.Lock()

    def start(self) -> bool:
        from services.websocket_client import WebSocketClient

        if self.client is not None and self.client.connected:
            return True
        host = "127.0.0.1"
        port = int(os.getenv("WEBSOCKET_PORT", "8765"))
        client = WebSocketClient(self.api_key, host=host, port=port)
        if not client.connect():
            logger.error(f"Alert feed for user {self.user_id} could not connect to the proxy")
            return False
        client.register_callback("market_data", self._on_market_data)
        self.client = client
        return True

    def _on_market_data(self, message: dict) -> None:
        """Tick callback from the WebSocketClient thread."""
        try:
            data = message.get("data") or {}
            ltp = data.get("ltp")
            if ltp is None:
                return
            ts_ms = int(data.get("timestamp") or data.get("time") or time.time() * 1000)
            self.on_tick(
                message.get("symbol"),
                message.get("exchange"),
                float(ltp),
                ts_ms,
            )
        except Exception:
            logger.exception("Alert feed tick handling failed")

    def subscribe(self, symbol: str, exchange: str) -> bool:
        if self.client is None and not self.start():
            return False
        result = self.client.subscribe([{"symbol": symbol, "exchange": exchange}], mode="Quote")
        status = result.get("status")
        if status not in ("success", "partial"):
            logger.warning(
                "Alert feed subscribe failed for %s %s: %s", exchange, symbol, result.get("message")
            )
            return False
        return True

    def shutdown(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                logger.exception("Alert feed disconnect failed")
            self.client = None


class AlertEngine:
    """Singleton: condition matching + webhook delivery for user alerts."""

    _instance: Optional["AlertEngine"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.lock = threading.Lock()
        # user_id -> AlertFeed (one WebSocket client per user)
        self.feeds: dict[str, AlertFeed] = {}
        # alert_id -> last seen price (for crossing operators)
        self.prev_prices: dict[str, float] = {}
        # alert_id -> api_key of the owner (for feed start / auth)
        self.alert_api_keys: dict[str, str] = {}
        self._started = False

    # ------------------------------------------------------------------ boot

    def start(self) -> None:
        """Idempotent boot: create tables if missing, reload ACTIVE alerts."""
        if self._started:
            return
        with self.lock:
            if self._started:
                return
            self._started = True
        try:
            alert_init_db()
            self.restore_alerts()
        except Exception:
            logger.exception("Alert engine start failed")
            self._started = False

    def restore_alerts(self) -> int:
        """Reload ACTIVE alerts after a restart; returns count restored.

        Persisted alerts keep working when the browser/editor is closed —
        this is the whole point of server-side alert persistence.
        """
        restored = 0
        alerts = []
        try:
            alerts = (
                db_session.query(Alert)
                .filter(Alert.status == "ACTIVE", Alert.enabled.is_(True))
                .all()
            )
        except Exception:
            logger.exception("Failed to load alerts for restore")
            return 0
        for alert in alerts:
            self._expire_if_past(alert)
            if alert.status == "ACTIVE" and alert.source_type == "price":
                api_key = self._api_key_for_user(alert.user_id)
                if api_key:
                    self._ensure_subscribed(alert, api_key)
                    restored += 1
                else:
                    logger.warning(
                        "Alert %s: no API key for user %s; price alerts paused until restart",
                        alert.id,
                        alert.user_id,
                    )
            elif alert.status == "ACTIVE":
                restored += 1
        if alerts:
            logger.info(f"Alert engine restored {restored} active alert(s)")
        return restored

    # ------------------------------------------------------- alert lifecycle

    def register_alert(self, alert_id: str, user_id: str, api_key: str) -> None:
        """Start market-data evaluation for a (new or re-enabled) alert."""
        alert = self.get_alert(alert_id)
        if alert is None:
            return
        with self.lock:
            self.alert_api_keys[alert_id] = api_key
        if alert.source_type == "price" and alert.status == "ACTIVE" and alert.enabled:
            self._ensure_subscribed(alert, api_key)

    def unregister_alert(
        self,
        alert_id: str,
        user_id: str | None = None,
        symbol: str | None = None,
        exchange: str | None = None,
    ) -> None:
        """Stop tracking an alert; release its feed subscription if last.

        Passing the alert's (user_id, symbol, exchange) lets the engine drop
        the WebSocket-proxy subscription once no active price alert needs it.
        """
        with self.lock:
            self.alert_api_keys.pop(alert_id, None)
            self.prev_prices.pop(alert_id, None)
        if not (user_id and symbol and exchange):
            return
        still_needed = False
        try:
            still_needed = (
                db_session.query(Alert.id)
                .filter(
                    Alert.user_id == user_id,
                    Alert.symbol == symbol,
                    Alert.exchange == exchange,
                    Alert.source_type == "price",
                    Alert.status == "ACTIVE",
                    Alert.enabled.is_(True),
                )
                .first()
                is not None
            )
        except Exception:
            logger.exception("Alert feed cleanup check failed")
            return
        if still_needed:
            return
        with self.lock:
            feed = self.feeds.get(user_id)
        if feed is None:
            return
        try:
            if feed.client is not None:
                feed.client.unsubscribe([{"symbol": symbol, "exchange": exchange}])
        except Exception:
            logger.exception("Alert feed unsubscribe failed")
        with self.lock:
            # Drop the whole feed once the user has no remaining subscriptions
            if feed.client is None or not feed.client.active_subscriptions:
                self.feeds.pop(user_id, None)
                feed.shutdown()

    # ------------------------------------------------------- event handlers

    def handle_pine_signal(self, event) -> None:
        """Bus subscriber for ``pine.signal``: match strategy-source alerts.

        The Pine runtime publishes the event; everything below is alert
        matching/filtering/dedup/persistence/delivery — the runtime itself
        never touches a webhook.
        """
        try:
            self._match_strategy_signal(event)
        except Exception:
            logger.exception("Alert engine failed to handle pine signal")

    def handle_price_tick(
        self, user_id: str, symbol: str, exchange: str, price: float, ts_ms: int
    ) -> None:
        """Tick callback for one user's price alerts."""
        try:
            alerts = (
                db_session.query(Alert)
                .filter(
                    Alert.user_id == user_id,
                    Alert.symbol == symbol,
                    Alert.exchange == exchange,
                    Alert.source_type == "price",
                    Alert.status == "ACTIVE",
                    Alert.enabled.is_(True),
                )
                .all()
            )
            for alert in alerts:
                self._evaluate_price_alert(alert, price, ts_ms)
        except Exception:
            logger.exception("Alert engine price evaluation failed")

    # --------------------------------------------------------- internal API

    def get_alert(self, alert_id: str) -> Alert | None:
        try:
            return db_session.query(Alert).filter(Alert.id == alert_id).first()
        except Exception:
            logger.exception("Alert lookup failed")
            return None

    def _expire_if_past(self, alert: Alert) -> bool:
        """Mark expired alerts so they can never trigger afterwards."""
        if alert.expiration is None:
            return False
        if alert.expiration <= _now():
            alert.status = "EXPIRED"
            alert.enabled = False
            self._commit()
            self.unregister_alert(alert.id, alert.user_id, alert.symbol, alert.exchange)
            logger.info(f"Alert {alert.id} expired")
            return True
        return False

    def _api_key_for_user(self, user_id: str) -> str | None:
        """Fetch the user's stored broker API key (existing auth store)."""
        try:
            from database.auth_db import get_api_key_for_tradingview

            return get_api_key_for_tradingview(user_id)
        except Exception:
            logger.exception("Alert engine could not fetch API key for user")
            return None

    def _ensure_subscribed(self, alert: Alert, api_key: str) -> None:
        with self.lock:
            feed = self.feeds.get(alert.user_id)
            if feed is None:
                feed = AlertFeed(alert.user_id, api_key, self._make_tick_handler(alert.user_id))
                self.feeds[alert.user_id] = feed
        if feed.api_key != api_key:
            feed.api_key = api_key
        feed.subscribe(alert.symbol, alert.exchange)

    def _make_tick_handler(self, user_id: str):
        def handler(symbol, exchange, price, ts_ms):
            self.handle_price_tick(user_id, symbol, exchange, price, ts_ms)

        return handler

    def _match_strategy_signal(self, event) -> None:
        """Match one PineSignalEvent against strategy-source alerts."""
        strategy_id = getattr(event, "strategy_id", "")
        user_id = self._user_for_strategy(strategy_id)
        if user_id is None:
            return
        alerts = (
            db_session.query(Alert)
            .filter(
                Alert.user_id == user_id,
                Alert.source_type == "strategy",
                Alert.status == "ACTIVE",
                Alert.enabled.is_(True),
            )
            .all()
        )
        signal = (getattr(event, "signal", "") or "").upper()
        for alert in alerts:
            if alert.strategy_id and alert.strategy_id != strategy_id:
                continue
            wanted = (alert.signal or "ANY").upper()
            if wanted != "ANY" and wanted != signal:
                continue
            self._expire_if_past(alert)
            if alert.status != "ACTIVE":
                continue
            bar_time = float(getattr(event, "bar_time", 0) or 0) or time.time() * 1000
            payload = {
                "event": "strategy_signal",
                "signal": signal,
                "symbol": getattr(event, "symbol", alert.symbol),
                "exchange": getattr(event, "exchange", alert.exchange),
                "timeframe": getattr(event, "timeframe", alert.timeframe),
                "price": float(getattr(event, "price", 0) or 0),
                "strategy": getattr(event, "strategy_name", ""),
                "strategy_id": strategy_id,
                "alert_id": alert.id,
                "mode": getattr(event, "mode", ""),
                "message": alert.message or f"{signal} signal on {alert.symbol}",
            }
            self._fire(alert, payload, bar_time, source="strategy", signal=signal)

    def _user_for_strategy(self, strategy_id: str) -> str | None:
        try:
            from database.pine_db import PineStrategyInstance

            inst = (
                db_session.query(PineStrategyInstance)
                .filter(PineStrategyInstance.id == strategy_id)
                .first()
            )
            return inst.user_id if inst else None
        except Exception:
            logger.exception("Alert engine could not resolve strategy owner")
            return None

    def _evaluate_price_alert(self, alert: Alert, price: float, ts_ms: int) -> None:
        if self._expire_if_past(alert):
            return
        operator = alert.operator or "greater_than"
        target = alert.value if alert.value is not None else 0.0
        prev = self.prev_prices.get(alert.id)
        with self.lock:
            self.prev_prices[alert.id] = price
        if prev is None:
            # First tick after (re)start: crossing needs a reference price.
            if operator in ("crossing", "crossing_up", "crossing_down"):
                return
        if not _price_condition_met(operator, prev, price, target):
            return
        payload = {
            "event": "price_cross",
            "signal": operator,
            "symbol": alert.symbol,
            "exchange": alert.exchange,
            "timeframe": alert.timeframe,
            "price": float(price),
            "operator": operator,
            "value": float(target),
            "alert_id": alert.id,
            "message": alert.message
            or f"{alert.symbol} {operator.replace('_', ' ')} {target:g}",
        }
        self._fire(alert, payload, ts_ms, source="price", signal=operator)

    # ------------------------------------------------------------- delivery

    def _fire(
        self, alert: Alert, payload: dict, bar_time: float, source: str, signal: str
    ) -> None:
        """Record one logical alert event (idempotent) and queue delivery."""
        key = make_idempotency_key(alert.id, source, bar_time, signal)
        payload["idempotency_key"] = key
        payload["bar_time"] = _ist_iso(bar_time)
        payload["timestamp"] = _ist_iso(time.time() * 1000)
        event_id = None
        try:
            existing = (
                db_session.query(AlertEvent)
                .filter(AlertEvent.idempotency_key == key)
                .first()
            )
            if existing is not None:
                # Already processed (e.g. duplicate signal after restart):
                # never deliver the same logical event twice.
                return
            event_id = uuid.uuid4().hex
            event_row = AlertEvent(
                id=event_id,
                alert_id=alert.id,
                event_type=payload["event"],
                signal=signal,
                symbol=alert.symbol,
                price=payload.get("price"),
                bar_time=bar_time,
                idempotency_key=key,
                payload=json.dumps(payload, default=str),
            )
            db_session.add(event_row)
            delivery = AlertDelivery(
                alert_event_id=event_id,
                alert_id=alert.id,
                webhook_url=alert.webhook_url,
                status="PENDING",
                attempt=0,
            )
            db_session.add(delivery)
            # Once-only lifecycle: the alert has fired; disable further triggers
            # regardless of webhook outcome (delivery retries continue).
            if alert.trigger_mode == "once_only":
                alert.status = "TRIGGERED"
                alert.enabled = False
                alert.last_triggered_at = _now()
            db_session.commit()
            delivery_id = delivery.id
        except Exception:
            logger.exception(f"Alert {alert.id}: failed to record event")
            db_session.rollback()
            return
        # Notify the UI (Socket.IO) outside the DB session.
        try:
            bus.publish(
                AlertTriggeredEvent(
                    alert_id=alert.id,
                    user_id=alert.user_id,
                    symbol=alert.symbol,
                    event_type=payload["event"],
                    signal=signal,
                    price=payload.get("price"),
                    message=payload.get("message"),
                )
            )
        except Exception:
            logger.exception("Alert triggered event publish failed")
        payload["event_id"] = event_id
        _DELIVERY_POOL.submit(
            self._deliver, delivery_id, event_id, alert.id, alert.webhook_url, payload
        )

    def _deliver(
        self, delivery_id: int, event_id: str, alert_id: str, url: str, payload: dict
    ) -> None:
        """Worker: POST the payload with retries; one delivery row per event."""
        headers = {
            "Content-Type": "application/json",
            "X-OpenAlgo-Event-ID": event_id,
            "X-OpenAlgo-Idempotency-Key": payload.get("idempotency_key", event_id),
            "User-Agent": "OpenAlgo-Alerts/1.0",
        }
        last_error = None
        last_status = None
        for attempt in range(1, len(RETRY_DELAYS) + 1):
            self._update_delivery(
                delivery_id,
                status="SENDING" if attempt == 1 else "RETRYING",
                attempt=attempt,
            )
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=WEBHOOK_TIMEOUT,
                )
                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    self._update_delivery(
                        delivery_id,
                        status="SUCCESS",
                        attempt=attempt,
                        http_status=response.status_code,
                        error=None,
                        completed=True,
                    )
                    self._publish_delivery(alert_id, event_id, "SUCCESS", response.status_code, None)
                    return
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc) or exc.__class__.__name__
                last_status = None
            logger.warning(
                f"Alert {alert_id} webhook attempt {attempt}/{len(RETRY_DELAYS)} failed: {last_error}"
            )
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
        self._update_delivery(
            delivery_id,
            status="FAILED",
            attempt=len(RETRY_DELAYS),
            http_status=last_status,
            error=last_error,
            completed=True,
        )
        self._publish_delivery(alert_id, event_id, "FAILED", last_status, last_error)

    def _update_delivery(self, delivery_id: int, **fields) -> None:
        try:
            row = db_session.query(AlertDelivery).filter(AlertDelivery.id == delivery_id).first()
            if row is None:
                return
            for k, v in fields.items():
                if k == "completed":
                    row.completed_at = _now() if v else None
                else:
                    setattr(row, k, v)
            db_session.commit()
        except Exception:
            logger.exception(f"Delivery {delivery_id}: status update failed")
            db_session.rollback()

    def _publish_delivery(
        self, alert_id: str, event_id: str, status: str, http_status: int | None, error: str | None
    ) -> None:
        try:
            bus.publish(
                AlertDeliveryEvent(
                    alert_id=alert_id,
                    event_id=event_id,
                    status=status,
                    http_status=http_status,
                    error=error,
                )
            )
        except Exception:
            logger.exception("Alert delivery event publish failed")

    def _commit(self) -> None:
        try:
            db_session.commit()
        except Exception:
            logger.exception("Alert engine commit failed")
            db_session.rollback()

    # ------------------------------------------------------------ utilities

    def test_webhook(self, url: str, user_id: str) -> dict[str, Any]:
        """Send a test event to a webhook URL. Never creates orders."""
        ok, reason = validate_webhook_url(url)
        if not ok:
            return {"status": "error", "message": reason, "http_status": None}
        payload = {
            "event": "test",
            "source": "openalgo",
            "alert_id": None,
            "user_id": user_id,
            "timestamp": _ist_iso(time.time() * 1000),
            "message": "Test webhook from OpenAlgo alerts — no order was created",
        }
        try:
            response = requests.post(url, json=payload, timeout=WEBHOOK_TIMEOUT)
            return {
                "status": "success" if 200 <= response.status_code < 300 else "error",
                "message": f"HTTP {response.status_code}",
                "http_status": response.status_code,
            }
        except requests.RequestException as exc:
            return {
                "status": "error",
                "message": str(exc) or exc.__class__.__name__,
                "http_status": None,
            }

    def shutdown(self) -> None:
        with self.lock:
            feeds = list(self.feeds.values())
            self.feeds.clear()
        for feed in feeds:
            feed.shutdown()


def _price_condition_met(operator: str, prev: float | None, price: float, target: float) -> bool:
    """Evaluate one price operator. ``prev`` is None on the first tick."""
    if operator == "greater_than":
        return price > target
    if operator == "less_than":
        return price < target
    if operator == "greater_than_equal":
        return price >= target
    if operator == "less_than_equal":
        return price <= target
    if prev is None:
        return False
    crossed_up = prev <= target < price
    crossed_down = prev >= target > price
    if operator == "crossing":
        return crossed_up or crossed_down
    if operator == "crossing_up":
        return crossed_up
    if operator == "crossing_down":
        return crossed_down
    return False


#: Module-level singleton (mirrors ``flow_price_monitor_service`` pattern).
alert_engine = AlertEngine()


def _shutdown_at_exit() -> None:
    try:
        alert_engine.shutdown()
    except Exception:
        logger.exception("Alert engine shutdown failed")


atexit.register(_shutdown_at_exit)
