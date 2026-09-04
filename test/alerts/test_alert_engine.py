"""Alert engine tests: matching, dedup, webhook delivery, SSRF, restore.

Uses a real local HTTP server as the webhook target (private URLs are allowed
via the engine's ALLOW_PRIVATE_WEBHOOKS flag) so the whole pipeline —
condition match -> event row -> delivery worker -> HTTP POST -> status row —
runs exactly as in production, with retry delays shortened to zero.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import database.alert_db as alert_db
import database.pine_db as pine_db
import services.alert_engine as alert_engine_mod
from database.alert_db import Alert, AlertDelivery, AlertEvent
from database.pine_db import PineStrategyInstance
from events.pine_events import PineSignalEvent
from services.alert_engine import (
    AlertEngine,
    _price_condition_met,
    alert_engine,
    make_idempotency_key,
    validate_webhook_url,
)

USER = "tester"
SYMBOL = "NIFTY"
EXCHANGE = "NSE"
TIMEFRAME = "5m"


class _Recorder:
    def __init__(self):
        self.requests: list[dict] = []
        self.fail_times = 0  # respond 500 to the first N posts
        self.lock = threading.Lock()

    def reset(self, fail_times=0):
        with self.lock:
            self.requests = []
            self.fail_times = fail_times


RECORDER = _Recorder()


class _WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        with RECORDER.lock:
            RECORDER.requests.append(
                {
                    "path": self.path,
                    "body": json.loads(body) if body else {},
                    "event_id": self.headers.get("X-OpenAlgo-Event-ID"),
                    "idempotency_key": self.headers.get("X-OpenAlgo-Idempotency-Key"),
                }
            )
            fail = RECORDER.fail_times > 0
            if fail:
                RECORDER.fail_times -= 1
        code = 500 if fail else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence test output
        return


@pytest.fixture(scope="module")
def webhook_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Point every alert/pine db reference at a temp sqlite."""
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'alert-engine-test.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    # alert_db module + the copies imported by name into alert_engine/blueprints
    monkeypatch.setattr(alert_db, "engine", test_engine)
    monkeypatch.setattr(alert_db, "db_session", test_session)
    monkeypatch.setattr(alert_engine_mod, "db_session", test_session)
    alert_db.Base.metadata.create_all(bind=test_engine)
    pine_db.Base.metadata.create_all(bind=test_engine)
    # Fast retries, short timeout, private webhooks allowed (local test server)
    monkeypatch.setattr(alert_engine_mod, "RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(alert_engine_mod, "WEBHOOK_TIMEOUT", 5)
    monkeypatch.setattr(alert_engine_mod, "ALLOW_PRIVATE_WEBHOOKS", True)
    # Fresh engine state per test (singleton keeps feeds/prev prices)
    alert_engine.feeds.clear()
    alert_engine.prev_prices.clear()
    alert_engine.alert_api_keys.clear()
    alert_engine._started = True  # skip table init path; tables already exist
    RECORDER.reset()
    yield test_session
    test_session.remove()
    test_engine.dispose()


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _wait_requests(n, timeout=10.0):
    return _wait_for(lambda: len(RECORDER.requests) >= n, timeout)


def make_price_alert(**overrides) -> Alert:
    defaults = {
        "id": "alert-price-1",
        "user_id": USER,
        "name": "NIFTY crossing up 22000",
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "timeframe": TIMEFRAME,
        "source_type": "price",
        "operator": "crossing_up",
        "value": 22000.0,
        "trigger_mode": "once_only",
        "expiration": None,
        "message": None,
        "webhook_url": "http://example.com/hook",
        "status": "ACTIVE",
        "enabled": True,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    defaults.update(overrides)
    alert = Alert(**defaults)
    alert_db.db_session.add(alert)
    alert_db.db_session.commit()
    return alert


def make_strategy_alert(**overrides) -> Alert:
    defaults = {
        "id": "alert-strategy-1",
        "user_id": USER,
        "name": "EMA Cross BUY",
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "timeframe": TIMEFRAME,
        "source_type": "strategy",
        "strategy_id": "inst-1",
        "signal": "BUY",
        "trigger_mode": "once_only",
        "expiration": None,
        "message": None,
        "webhook_url": "http://example.com/hook",
        "status": "ACTIVE",
        "enabled": True,
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    defaults.update(overrides)
    alert = Alert(**defaults)
    alert_db.db_session.add(alert)
    alert_db.db_session.commit()
    return alert


def make_signal(**overrides) -> PineSignalEvent:
    defaults = {
        "mode": "paper",
        "strategy_id": "inst-1",
        "strategy_name": "EMA Cross",
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "timeframe": TIMEFRAME,
        "signal": "BUY",
        "kind": "entry",
        "price": 22050.0,
        "quantity": 1,
        "bar_time": 1700000000000.0,
        "bar_index": 10,
        "source": "realtime",
    }
    defaults.update(overrides)
    return PineSignalEvent(**defaults)


def make_instance(instance_id="inst-1", user_id=USER) -> PineStrategyInstance:
    inst = PineStrategyInstance(
        id=instance_id,
        user_id=user_id,
        script_id=1,
        name="EMA Cross",
        symbol=SYMBOL,
        exchange=EXCHANGE,
        timeframe=TIMEFRAME,
        quantity=1,
        product="MIS",
        execution_mode="PAPER",
        status="RUNNING",
        inputs="{}",
    )
    alert_db.db_session.add(inst)
    alert_db.db_session.commit()
    return inst


# ---------------------------------------------------------------------------
# Price operators (spec: 7 operators)
# ---------------------------------------------------------------------------


class TestPriceOperators:
    def test_crossing_up(self):
        assert _price_condition_met("crossing_up", 21990, 22010, 22000)

    def test_crossing_up_no_trigger_when_staying_above(self):
        assert not _price_condition_met("crossing_up", 22010, 22030, 22000)

    def test_crossing_down(self):
        assert _price_condition_met("crossing_down", 22010, 21990, 22000)

    def test_crossing_either_direction(self):
        assert _price_condition_met("crossing", 21990, 22010, 22000)
        assert _price_condition_met("crossing", 22010, 21990, 22000)
        assert not _price_condition_met("crossing", 22010, 22030, 22000)

    def test_greater_than(self):
        assert _price_condition_met("greater_than", None, 22001, 22000)
        assert not _price_condition_met("greater_than", None, 22000, 22000)

    def test_less_than(self):
        assert _price_condition_met("less_than", None, 21999, 22000)
        assert not _price_condition_met("less_than", None, 22000, 22000)

    def test_greater_than_equal(self):
        assert _price_condition_met("greater_than_equal", None, 22000, 22000)
        assert not _price_condition_met("greater_than_equal", None, 21999, 22000)

    def test_less_than_equal(self):
        assert _price_condition_met("less_than_equal", None, 22000, 22000)
        assert not _price_condition_met("less_than_equal", None, 22001, 22000)

    def test_crossing_needs_previous_tick(self):
        assert not _price_condition_met("crossing_up", None, 22010, 22000)

    def test_operator_validation_in_blueprint(self):
        # blueprint-level validation is covered in test_alerts_api.py; here we
        # only assert the engine-side set stays complete.
        assert alert_engine_mod.PRICE_OPERATORS == {
            "crossing",
            "crossing_up",
            "crossing_down",
            "greater_than",
            "less_than",
            "greater_than_equal",
            "less_than_equal",
        }


# ---------------------------------------------------------------------------
# Price alert trigger flow (tick -> event -> webhook)
# ---------------------------------------------------------------------------


class TestPriceAlertFlow:
    def test_tick_triggers_webhook_with_payload_and_headers(self, webhook_server):
        alert = make_price_alert(webhook_url=webhook_server)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21990, 1700000000000)
        assert not RECORDER.requests  # below target, crossing needs reference
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22010, 1700000001000)
        assert _wait_requests(1)
        body = RECORDER.requests[0]["body"]
        assert body["event"] == "price_cross"
        assert body["symbol"] == SYMBOL
        assert body["exchange"] == EXCHANGE
        assert body["timeframe"] == TIMEFRAME
        assert body["price"] == 22010
        assert body["operator"] == "crossing_up"
        assert body["value"] == 22000
        assert body["alert_id"] == "alert-price-1"
        assert body["event_id"] == RECORDER.requests[0]["event_id"]
        assert body["idempotency_key"] == RECORDER.requests[0]["idempotency_key"]
        assert "bar_time" in body and "timestamp" in body
        assert _wait_for(
            lambda: alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == alert.id)
            .first()
            .status
            == "SUCCESS"
        )

    def test_trigger_once_only_lifecycle(self, webhook_server):
        make_price_alert(id="alert-once", webhook_url=webhook_server)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21990, 1700000000000)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22010, 1700000001000)
        assert _wait_requests(1)
        row = alert_db.db_session.query(Alert).filter(Alert.id == "alert-once").first()
        assert row.status == "TRIGGERED"
        assert row.enabled is False
        # Price crosses again: no second event, no second webhook
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21980, 1700000002000)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22020, 1700000003000)
        time.sleep(0.3)
        assert len(RECORDER.requests) == 1
        assert alert_db.db_session.query(AlertEvent).count() == 1

    def test_expired_alert_never_triggers(self, webhook_server):
        make_price_alert(
            id="alert-expired",
            webhook_url=webhook_server,
            expiration=datetime.now(UTC) - timedelta(minutes=5),
        )
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21990, 1700000000000)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22010, 1700000001000)
        time.sleep(0.3)
        row = alert_db.db_session.query(Alert).filter(Alert.id == "alert-expired").first()
        assert row.status == "EXPIRED"
        assert len(RECORDER.requests) == 0
        assert alert_db.db_session.query(AlertEvent).count() == 0

    def test_disabled_alert_never_triggers(self, webhook_server):
        make_price_alert(id="alert-off", webhook_url=webhook_server, enabled=False)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21990, 1700000000000)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22010, 1700000001000)
        time.sleep(0.3)
        assert len(RECORDER.requests) == 0

    def test_other_users_alert_not_triggered_by_this_user_tick(self, webhook_server):
        make_price_alert(id="alert-other", user_id="someone-else", webhook_url=webhook_server)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21990, 1700000000000)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22010, 1700000001000)
        time.sleep(0.3)
        assert len(RECORDER.requests) == 0

    def test_multiple_alerts_independent(self, webhook_server):
        make_price_alert(id="alert-a", webhook_url=webhook_server, value=22000)
        make_price_alert(
            id="alert-b",
            webhook_url=webhook_server,
            value=22500,
            operator="greater_than",
        )
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 21990, 1700000000000)
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22010, 1700000001000)
        assert _wait_requests(1)
        # Only alert-a matched so far
        assert alert_db.db_session.query(Alert).filter(Alert.id == "alert-a").first().status == "TRIGGERED"
        assert alert_db.db_session.query(Alert).filter(Alert.id == "alert-b").first().status == "ACTIVE"
        alert_engine.handle_price_tick(USER, SYMBOL, EXCHANGE, 22510, 1700000002000)
        assert _wait_requests(2)
        assert alert_db.db_session.query(Alert).filter(Alert.id == "alert-b").first().status == "TRIGGERED"


# ---------------------------------------------------------------------------
# Strategy signal flow (pine.signal -> engine)
# ---------------------------------------------------------------------------


class TestStrategyAlertFlow:
    def test_buy_signal_fires_matching_alert(self, webhook_server):
        make_instance()
        alert = make_strategy_alert(webhook_url=webhook_server)
        alert_engine.handle_pine_signal(make_signal())
        assert _wait_requests(1)
        body = RECORDER.requests[0]["body"]
        assert body["event"] == "strategy_signal"
        assert body["signal"] == "BUY"
        assert body["strategy"] == "EMA Cross"
        assert body["strategy_id"] == "inst-1"
        assert body["symbol"] == SYMBOL
        assert body["exchange"] == EXCHANGE
        assert body["timeframe"] == TIMEFRAME
        assert body["price"] == 22050.0
        assert body["alert_id"] == alert.id
        assert RECORDER.requests[0]["event_id"] == body["event_id"]

    def test_wrong_signal_does_not_match(self, webhook_server):
        make_instance()
        make_strategy_alert(id="alert-buy", webhook_url=webhook_server, signal="BUY")
        alert_engine.handle_pine_signal(make_signal(signal="SELL"))
        time.sleep(0.3)
        assert len(RECORDER.requests) == 0

    def test_any_signal_matches_both(self, webhook_server):
        make_instance()
        make_strategy_alert(id="alert-any", webhook_url=webhook_server, signal="ANY")
        alert_engine.handle_pine_signal(make_signal(signal="BUY", bar_time=1700000001000.0))
        assert _wait_requests(1)
        RECORDER.reset()
        make_strategy_alert(id="alert-any2", webhook_url=webhook_server, signal="ANY")
        alert_engine.handle_pine_signal(make_signal(signal="SELL", bar_time=1700000002000.0))
        assert _wait_requests(1)

    def test_wrong_strategy_does_not_match(self, webhook_server):
        make_instance("inst-1")
        make_instance("inst-2")
        make_strategy_alert(id="alert-strat", webhook_url=webhook_server, strategy_id="inst-2")
        alert_engine.handle_pine_signal(make_signal(strategy_id="inst-1"))
        time.sleep(0.3)
        assert len(RECORDER.requests) == 0

    def test_duplicate_signal_is_deduplicated(self, webhook_server):
        make_instance()
        make_strategy_alert(id="alert-dup", webhook_url=webhook_server)
        event = make_signal(bar_time=1700000000000.0)
        alert_engine.handle_pine_signal(event)
        assert _wait_requests(1)
        # Same bar + signal replayed (e.g. after restart): no duplicate
        alert_engine.handle_pine_signal(event)
        time.sleep(0.5)
        assert len(RECORDER.requests) == 1
        assert alert_db.db_session.query(AlertEvent).count() == 1

    def test_pine_runtime_emits_no_webhook_directly(self):
        """The pine runtime module must not import requests/post webhooks."""
        import inspect

        import pine.runtime as runtime_mod

        source = inspect.getsource(runtime_mod)
        assert "requests.post" not in source
        assert "webhook" not in source.lower()


# ---------------------------------------------------------------------------
# Delivery semantics: retry, failure isolation, timeout
# ---------------------------------------------------------------------------


class TestDelivery:
    def test_retry_then_success_single_event(self, webhook_server):
        make_instance()
        make_strategy_alert(id="alert-retry", webhook_url=webhook_server)
        RECORDER.reset(fail_times=1)  # first attempt 500, second 200
        alert_engine.handle_pine_signal(make_signal(bar_time=1700000000000.0))
        assert _wait_requests(2)
        assert _wait_for(
            lambda: alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == "alert-retry")
            .first()
            .status
            == "SUCCESS"
        )
        delivery = (
            alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == "alert-retry")
            .first()
        )
        assert delivery.attempt == 2
        assert delivery.http_status == 200  # status of the successful attempt
        assert delivery.error is None
        assert alert_db.db_session.query(AlertEvent).count() == 1

    def test_max_three_attempts_then_failed(self, webhook_server):
        make_instance()
        make_strategy_alert(id="alert-fail", webhook_url=webhook_server)
        RECORDER.reset(fail_times=99)
        alert_engine.handle_pine_signal(make_signal(bar_time=1700000000000.0))
        assert _wait_requests(3)
        assert _wait_for(
            lambda: alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == "alert-fail")
            .first()
            .status
            == "FAILED"
        )
        delivery = (
            alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == "alert-fail")
            .first()
        )
        assert delivery.attempt == 3
        assert len(RECORDER.requests) == 3
        # Same idempotency key on every attempt
        keys = {r["idempotency_key"] for r in RECORDER.requests}
        assert len(keys) == 1

    def test_failed_webhook_does_not_stop_other_alerts(self, webhook_server):
        make_instance()
        # alert-fail targets a dead port; alert-ok targets the mock server
        dead_port = 1  # unprivileged, nothing listens here
        make_strategy_alert(
            id="alert-dead",
            webhook_url=f"http://127.0.0.1:{dead_port}/hook",
            signal="ANY",
        )
        make_strategy_alert(
            id="alert-ok",
            webhook_url=webhook_server,
            signal="ANY",
        )
        alert_engine.handle_pine_signal(make_signal(signal="BUY", bar_time=1700000000000.0))
        assert _wait_requests(1)
        body = RECORDER.requests[0]["body"]
        assert body["alert_id"] == "alert-ok"
        assert _wait_for(
            lambda: alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == "alert-dead")
            .first()
            .status
            == "FAILED"
        )
        assert (
            alert_db.db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_id == "alert-ok")
            .first()
            .status
            == "SUCCESS"
        )

    def test_webhook_timeout_is_bounded(self, webhook_server, monkeypatch):
        monkeypatch.setattr(alert_engine_mod, "WEBHOOK_TIMEOUT", 1)
        # Handler that sleeps longer than the timeout
        import socketserver

        class _SlowHandler(_WebhookHandler):
            def do_POST(self):  # noqa: N802
                time.sleep(3)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = socketserver.TCPServer(("127.0.0.1", 0), _SlowHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        slow_url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        try:
            make_instance()
            make_strategy_alert(id="alert-slow", webhook_url=slow_url)
            started = time.time()
            alert_engine.handle_pine_signal(make_signal(bar_time=1700000000000.0))
            assert _wait_for(
                lambda: alert_db.db_session.query(AlertDelivery)
                .filter(AlertDelivery.alert_id == "alert-slow")
                .first()
                .status
                == "FAILED",
                timeout=25,
            )
            # 3 attempts x (1s timeout + 0 delay) < 25s: bounded, not hanging
            assert time.time() - started < 20
            delivery = (
                alert_db.db_session.query(AlertDelivery)
                .filter(AlertDelivery.alert_id == "alert-slow")
                .first()
            )
            assert "timed out" in (delivery.error or "").lower()
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# Idempotency + SSRF
# ---------------------------------------------------------------------------


class TestIdempotencyAndSSRF:
    def test_idempotency_key_is_deterministic(self):
        a = make_idempotency_key("alert-1", "strategy", 1700000000000, "BUY")
        b = make_idempotency_key("alert-1", "strategy", 1700000000000, "BUY")
        c = make_idempotency_key("alert-1", "strategy", 1700000000001, "BUY")
        d = make_idempotency_key("alert-2", "strategy", 1700000000000, "BUY")
        e = make_idempotency_key("alert-1", "price", 1700000000000, "BUY")
        f = make_idempotency_key("alert-1", "strategy", 1700000000000, "SELL")
        assert a == b
        assert len({a, c, d, e, f}) == 5

    def test_unique_index_on_idempotency_key(self):
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(alert_db.engine)
        indexes = [i for t in ("alert_events",) for i in inspector.get_indexes(t)]
        unique_cols = {
            tuple(i["column_names"]) for i in indexes if i.get("unique")
        }
        assert ("idempotency_key",) in unique_cols

    def test_ssrf_blocks_localhost(self, monkeypatch):
        monkeypatch.setattr(alert_engine_mod, "ALLOW_PRIVATE_WEBHOOKS", False)
        for url in (
            "http://localhost/hook",
            "http://127.0.0.1:5000/hook",
            "https://0.0.0.0/hook",
            "http://[::1]/hook",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/hook",
            "http://172.16.1.5/hook",
            "http://192.168.1.5/hook",
            "ftp://example.com/hook",
            "not a url",
            "",
        ):
            ok, _ = validate_webhook_url(url)
            assert not ok, url

    def test_ssrf_allows_public_https(self, monkeypatch):
        monkeypatch.setattr(alert_engine_mod, "ALLOW_PRIVATE_WEBHOOKS", False)
        ok, reason = validate_webhook_url("https://example.com/webhook?key=1")
        assert ok, reason

    def test_ssrf_private_allowed_when_configured(self):
        # ALLOW_PRIVATE_WEBHOOKS is True in this fixture (self-hosted override)
        ok, _ = validate_webhook_url("http://127.0.0.1:9000/hook")
        assert ok

    def test_webhook_payload_contains_no_secrets(self, webhook_server):
        import os

        make_instance()
        make_strategy_alert(id="alert-safe", webhook_url=webhook_server)
        alert_engine.handle_pine_signal(make_signal(bar_time=1700000000000.0))
        assert _wait_requests(1)
        raw = json.dumps(RECORDER.requests[0]["body"])
        for env_name in ("BROKER_API_KEY", "APP_KEY", "DATABASE_URL", "SECRET"):
            value = os.environ.get(env_name, "")
            if value:
                assert value not in raw


# ---------------------------------------------------------------------------
# Test webhook + restore
# ---------------------------------------------------------------------------


class TestUtilityFlows:
    def test_test_webhook_sends_test_event(self, webhook_server):
        result = alert_engine.test_webhook(webhook_server, USER)
        assert result["status"] == "success"
        body = RECORDER.requests[0]["body"]
        assert body["event"] == "test"
        assert body["source"] == "openalgo"
        assert body["user_id"] == USER
        # A test webhook must never create an alert event or delivery
        assert alert_db.db_session.query(AlertEvent).count() == 0
        assert alert_db.db_session.query(AlertDelivery).count() == 0

    def test_restore_keeps_active_alerts(self, webhook_server, monkeypatch):
        make_instance()
        make_strategy_alert(id="alert-live", webhook_url=webhook_server)
        make_strategy_alert(id="alert-done", webhook_url=webhook_server, status="TRIGGERED", enabled=False)
        # Simulate process restart: fresh singleton state, then restore
        monkeypatch.setattr(
            alert_engine_mod.AlertEngine, "_api_key_for_user", lambda self, user: "key"
        )
        restored = alert_engine_mod.AlertEngine().restore_alerts()
        assert restored == 1
        # And it still fires after restore
        alert_engine.handle_pine_signal(make_signal(bar_time=1700000000000.0))
        assert _wait_requests(1)
        assert RECORDER.requests[0]["body"]["alert_id"] == "alert-live"

    def test_restore_expires_stale_alerts(self, webhook_server):
        make_price_alert(
            id="alert-stale",
            webhook_url=webhook_server,
            expiration=datetime.now(UTC) - timedelta(hours=1),
        )
        restored = alert_engine.restore_alerts()
        assert restored == 0
        row = alert_db.db_session.query(Alert).filter(Alert.id == "alert-stale").first()
        assert row.status == "EXPIRED"

    def test_singleton(self):
        assert AlertEngine() is alert_engine
