"""Blueprint API tests for /alerts endpoints.

Uses a minimal Flask app with the alerts blueprint and a valid fake session —
the same surface the React alert panel talks to. Covers CRUD, enable/disable,
logs, the test-webhook endpoint, validation errors, ownership and auth.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import blueprints.alerts as alerts_blueprint
import database.alert_db as alert_db
import services.alert_engine as alert_engine_mod
import utils.session as session_utils
from database.alert_db import Alert, AlertDelivery, AlertEvent

WEBHOOK = "https://example.com/webhook"

PRICE_BODY = {
    "symbol": "NIFTY",
    "exchange": "NSE",
    "timeframe": "5m",
    "source_type": "price",
    "operator": "crossing_up",
    "value": 22000,
    "webhook_url": WEBHOOK,
}

STRATEGY_BODY = {
    "symbol": "NIFTY",
    "exchange": "NSE",
    "timeframe": "5m",
    "source_type": "strategy",
    "strategy_id": "inst-1",
    "signal": "BUY",
    "webhook_url": WEBHOOK,
}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'alerts-api-test.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(alert_db, "engine", test_engine)
    monkeypatch.setattr(alert_db, "db_session", test_session)
    monkeypatch.setattr(alerts_blueprint, "db_session", test_session)
    alert_db.Base.metadata.create_all(bind=test_engine)
    # Deterministic SSRF checks regardless of a developer's local .env
    monkeypatch.setattr(alert_engine_mod, "ALLOW_PRIVATE_WEBHOOKS", False)
    # Engine never starts a feed in these tests (no API key configured).
    monkeypatch.setattr(
        alerts_blueprint, "get_api_key_for_tradingview", lambda user: None
    )
    yield test_session
    test_session.remove()
    test_engine.dispose()


@pytest.fixture()
def app(monkeypatch):
    application = Flask(__name__)
    application.config["TESTING"] = True
    application.secret_key = "alerts-test-secret"
    application.register_blueprint(alerts_blueprint.alerts_bp)
    monkeypatch.setattr(session_utils, "is_session_valid", lambda: True)
    return application


@pytest.fixture()
def client(app):
    with app.test_client() as test_client:
        with test_client.session_transaction() as session:
            session["user"] = "tester"
            session["logged_in"] = True
            session["login_time"] = datetime.now(UTC).isoformat()
        yield test_client


def seed_alert(**overrides) -> Alert:
    defaults = {
        "id": uuid.uuid4().hex,
        "user_id": "tester",
        "name": "NIFTY crossing up 22000",
        "symbol": "NIFTY",
        "exchange": "NSE",
        "timeframe": "5m",
        "source_type": "price",
        "operator": "crossing_up",
        "value": 22000.0,
        "trigger_mode": "once_only",
        "webhook_url": WEBHOOK,
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


class TestAuth:
    def test_unauthenticated_list_rejected(self, app, monkeypatch):
        monkeypatch.setattr(session_utils, "is_session_valid", lambda: False)
        with app.test_client() as anon:
            response = anon.get("/alerts", headers={"Accept": "application/json"})
            assert response.status_code == 401
            assert response.get_json()["status"] == "error"

    def test_unauthenticated_create_rejected(self, app, monkeypatch):
        monkeypatch.setattr(session_utils, "is_session_valid", lambda: False)
        with app.test_client() as anon:
            response = anon.post("/alerts", json=PRICE_BODY)
            assert response.status_code == 401


class TestCrud:
    def test_create_price_alert(self, client):
        response = client.post("/alerts", json=PRICE_BODY)
        assert response.status_code == 201
        body = response.get_json()
        assert body["status"] == "success"
        alert = body["alert"]
        assert alert["source_type"] == "price"
        assert alert["operator"] == "crossing_up"
        assert alert["value"] == 22000
        assert alert["status"] == "ACTIVE"
        assert alert["enabled"] is True
        assert alert["trigger_mode"] == "once_only"

    def test_create_strategy_alert(self, client):
        response = client.post("/alerts", json=STRATEGY_BODY)
        assert response.status_code == 201
        alert = response.get_json()["alert"]
        assert alert["source_type"] == "strategy"
        assert alert["strategy_id"] == "inst-1"
        assert alert["signal"] == "BUY"

    def test_list_only_own_alerts(self, client):
        mine = seed_alert(user_id="tester")
        seed_alert(user_id="someone-else", name="not mine")
        response = client.get("/alerts")
        assert response.status_code == 200
        alerts = response.get_json()["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["id"] == mine.id

    def test_get_alert(self, client):
        alert = seed_alert()
        response = client.get(f"/alerts/{alert.id}")
        assert response.status_code == 200
        assert response.get_json()["alert"]["id"] == alert.id

    def test_get_other_users_alert_404(self, client):
        alert = seed_alert(user_id="someone-else")
        response = client.get(f"/alerts/{alert.id}")
        assert response.status_code == 404

    def test_update_alert(self, client):
        alert = seed_alert()
        response = client.put(f"/alerts/{alert.id}", json={"value": 22500})
        assert response.status_code == 200
        assert response.get_json()["alert"]["value"] == 22500

    def test_update_triggered_alert_rejected(self, client):
        alert = seed_alert(status="TRIGGERED", enabled=False)
        response = client.put(f"/alerts/{alert.id}", json={"value": 22500})
        assert response.status_code == 400

    def test_delete_alert(self, client):
        alert = seed_alert()
        response = client.delete(f"/alerts/{alert.id}")
        assert response.status_code == 200
        assert client.get(f"/alerts/{alert.id}").status_code == 404

    def test_delete_missing_404(self, client):
        assert client.delete("/alerts/nope").status_code == 404


class TestEnableDisable:
    def test_disable(self, client):
        alert = seed_alert()
        response = client.post(f"/alerts/{alert.id}/disable")
        assert response.status_code == 200
        body = response.get_json()["alert"]
        assert body["enabled"] is False

    def test_enable(self, client):
        alert = seed_alert(enabled=False)
        response = client.post(f"/alerts/{alert.id}/enable")
        assert response.status_code == 200
        body = response.get_json()["alert"]
        assert body["enabled"] is True
        assert body["status"] == "ACTIVE"

    def test_enable_triggered_alert_rejected(self, client):
        alert = seed_alert(status="TRIGGERED", enabled=False)
        response = client.post(f"/alerts/{alert.id}/enable")
        assert response.status_code == 400
        assert "triggered" in response.get_json()["message"].lower()


class TestValidation:
    def test_bad_operator_rejected(self, client):
        body = {**PRICE_BODY, "operator": "between"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400
        assert "operator" in response.get_json()["message"].lower()

    def test_bad_exchange_rejected(self, client):
        body = {**PRICE_BODY, "exchange": "NASDAQ"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_bad_timeframe_rejected(self, client):
        body = {**PRICE_BODY, "timeframe": "7m"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_missing_value_rejected(self, client):
        body = {k: v for k, v in PRICE_BODY.items() if k != "value"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_non_numeric_value_rejected(self, client):
        body = {**PRICE_BODY, "value": "abc"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_bad_signal_rejected(self, client):
        body = {**STRATEGY_BODY, "signal": "HOLD"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_missing_strategy_id_rejected(self, client):
        body = {k: v for k, v in STRATEGY_BODY.items() if k != "strategy_id"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_local_webhook_url_rejected(self, client):
        for url in (
            "http://localhost:9000/hook",
            "http://127.0.0.1:9000/hook",
            "http://169.254.169.254/meta",
            "http://192.168.0.10/hook",
            "file:///etc/passwd",
        ):
            body = {**PRICE_BODY, "webhook_url": url}
            response = client.post("/alerts", json=body)
            assert response.status_code == 400, url

    def test_unsupported_trigger_mode_rejected(self, client):
        body = {**PRICE_BODY, "trigger_mode": "every_time"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400
        assert "once_only" in response.get_json()["message"]

    def test_bad_expiration_rejected(self, client):
        body = {**PRICE_BODY, "expiration": "tomorrow"}
        response = client.post("/alerts", json=body)
        assert response.status_code == 400

    def test_valid_expiration_accepted(self, client):
        body = {**PRICE_BODY, "expiration": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
        response = client.post("/alerts", json=body)
        assert response.status_code == 201
        assert response.get_json()["alert"]["expiration"] is not None


class TestLogs:
    def test_logs_shape(self, client):
        alert = seed_alert()
        event = AlertEvent(
            id="evt-1",
            alert_id=alert.id,
            event_type="price_cross",
            signal="crossing_up",
            symbol="NIFTY",
            price=22010.0,
            bar_time=1700000000000.0,
            idempotency_key="key-1",
            payload="{}",
            created_at=datetime.now(),
        )
        alert_db.db_session.add(event)
        alert_db.db_session.add(
            AlertDelivery(
                alert_event_id="evt-1",
                alert_id=alert.id,
                webhook_url=WEBHOOK,
                status="SUCCESS",
                attempt=1,
                http_status=200,
                created_at=datetime.now(),
            )
        )
        alert_db.db_session.commit()
        response = client.get(f"/alerts/{alert.id}/logs")
        assert response.status_code == 200
        logs = response.get_json()["logs"]
        assert len(logs) == 1
        assert logs[0]["idempotency_key"] == "key-1"
        assert logs[0]["deliveries"][0]["status"] == "SUCCESS"
        assert logs[0]["deliveries"][0]["http_status"] == 200

    def test_logs_empty(self, client):
        alert = seed_alert()
        response = client.get(f"/alerts/{alert.id}/logs")
        assert response.status_code == 200
        assert response.get_json()["logs"] == []

    def test_logs_other_user_404(self, client):
        alert = seed_alert(user_id="someone-else")
        assert client.get(f"/alerts/{alert.id}/logs").status_code == 404


class TestTestWebhook:
    def test_test_webhook_success(self, client, monkeypatch):
        monkeypatch.setattr(
            alerts_blueprint.alert_engine,
            "test_webhook",
            lambda url, user: {"status": "success", "message": "HTTP 200", "http_status": 200},
        )
        response = client.post("/alerts/test", json={"webhook_url": WEBHOOK})
        assert response.status_code == 200
        assert response.get_json()["status"] == "success"

    def test_test_webhook_invalid_url(self, client):
        response = client.post("/alerts/test", json={"webhook_url": "http://127.0.0.1:9000/x"})
        assert response.status_code == 400

    def test_test_webhook_creates_no_events(self, client, monkeypatch):
        """The real engine path must not record events or deliveries."""
        import requests as requests_mod

        def _fail(*args, **kwargs):
            raise requests_mod.ConnectionError("connection refused")

        monkeypatch.setattr(alert_engine_mod.requests, "post", _fail)
        response = client.post("/alerts/test", json={"webhook_url": WEBHOOK})
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "error"
        assert alert_db.db_session.query(AlertEvent).count() == 0
        assert alert_db.db_session.query(AlertDelivery).count() == 0


class TestEngineRegistration:
    def test_create_registers_with_engine(self, client, monkeypatch):
        registered = []
        monkeypatch.setattr(
            alerts_blueprint, "get_api_key_for_tradingview", lambda user: "key-1"
        )
        monkeypatch.setattr(
            alerts_blueprint.alert_engine,
            "register_alert",
            lambda alert_id, user_id, api_key: registered.append(alert_id),
        )
        response = client.post("/alerts", json=PRICE_BODY)
        assert response.status_code == 201
        assert len(registered) == 1

    def test_disable_unregisters(self, client, monkeypatch):
        unregistered = []
        monkeypatch.setattr(
            alerts_blueprint.alert_engine,
            "unregister_alert",
            lambda alert_id, *a, **kw: unregistered.append(alert_id),
        )
        alert = seed_alert()
        response = client.post(f"/alerts/{alert.id}/disable")
        assert response.status_code == 200
        assert unregistered == [alert.id]
