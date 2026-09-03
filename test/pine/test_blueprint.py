"""Blueprint API tests for /pine endpoints.

Uses a minimal Flask app with the pine blueprint and a valid fake session -
the same surface the React Pine editor talks to. Verifies the security-critical
behaviours: authentication, PAPER default, and the LIVE confirmation gate.
"""

import json
from datetime import UTC, datetime, timezone

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import blueprints.pine as pine_blueprint
import database.pine_db as pine_db
import utils.session as session_utils

EMA_CROSS = """//@version=5
strategy("EMA Cross", overlay=true)
fast = ta.ema(close, 3)
slow = ta.ema(close, 8)
if ta.crossover(fast, slow)
    strategy.entry("BUY", strategy.long)
if ta.crossunder(fast, slow)
    strategy.entry("SELL", strategy.short)
"""


@pytest.fixture(autouse=True)
def isolated_pine_database(tmp_path, monkeypatch):
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'pine-api-test.db'}",
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


@pytest.fixture()
def app(monkeypatch):
    application = Flask(__name__)
    application.config["TESTING"] = True
    application.secret_key = "pine-test-secret"
    application.register_blueprint(pine_blueprint.pine_bp)

    # Valid session for every request in these tests.
    monkeypatch.setattr(
        session_utils,
        "is_session_valid",
        lambda: True,
    )
    # The blueprint module bound check_session_validity at import time, but
    # the decorator calls utils.session.is_session_valid dynamically, so the
    # monkeypatch above takes effect.
    return application


@pytest.fixture()
def client(app):
    with app.test_client() as test_client:
        with test_client.session_transaction() as session:
            session["user"] = "tester"
            session["logged_in"] = True
            session["login_time"] = datetime.now(UTC).isoformat()
        yield test_client


class TestCompileEndpoint:
    def test_compile_success(self, client):
        response = client.post(
            "/pine/compile", json={"code": EMA_CROSS}, headers={"Accept": "application/json"}
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "success"
        assert body["title"] == "EMA Cross"
        assert body["kind"] == "strategy"

    def test_compile_error_with_position(self, client):
        code = '//@version=5\nstrategy("X")\nplot(unknownVar)\n'
        response = client.post("/pine/compile", json={"code": code})
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "error"
        assert body["error"]["type"] == "compile_error"
        assert body["error"]["line"] == 3

    def test_compile_empty_code(self, client):
        response = client.post("/pine/compile", json={"code": ""})
        assert response.status_code == 400


class TestScriptCrud:
    def test_save_and_load(self, client):
        response = client.post("/pine/scripts", json={"name": "My EMA", "code": EMA_CROSS})
        assert response.status_code == 200
        script_id = response.get_json()["id"]

        loaded = client.get(f"/pine/scripts/{script_id}")
        assert loaded.status_code == 200
        assert loaded.get_json()["script"]["code"] == EMA_CROSS

    def test_list(self, client):
        client.post("/pine/scripts", json={"name": "A", "code": EMA_CROSS})
        response = client.get("/pine/scripts")
        assert response.status_code == 200
        assert len(response.get_json()["scripts"]) == 1

    def test_update(self, client):
        script_id = client.post(
            "/pine/scripts", json={"name": "A", "code": EMA_CROSS}
        ).get_json()["id"]
        response = client.put(
            f"/pine/scripts/{script_id}", json={"name": "Renamed"}
        )
        assert response.status_code == 200
        loaded = client.get(f"/pine/scripts/{script_id}").get_json()
        assert loaded["script"]["name"] == "Renamed"

    def test_delete(self, client):
        script_id = client.post(
            "/pine/scripts", json={"name": "A", "code": EMA_CROSS}
        ).get_json()["id"]
        assert client.delete(f"/pine/scripts/{script_id}").status_code == 200
        assert client.get(f"/pine/scripts/{script_id}").status_code == 404

    def test_cannot_read_other_users_script(self, client, app):
        script_id = client.post(
            "/pine/scripts", json={"name": "A", "code": EMA_CROSS}
        ).get_json()["id"]
        with client.session_transaction() as session:
            session["user"] = "someone-else"
        assert client.get(f"/pine/scripts/{script_id}").status_code == 404


class TestStrategyLifecycle:
    def _create(self, client):
        script_id = client.post(
            "/pine/scripts", json={"name": "EMA Cross", "code": EMA_CROSS}
        ).get_json()["id"]
        response = client.post(
            "/pine/strategies",
            json={
                "script_id": script_id,
                "name": "EMA Cross",
                "symbol": "NIFTY",
                "exchange": "NSE",
                "timeframe": "1m",
                "quantity": 2,
                "product": "MIS",
            },
        )
        assert response.status_code == 200
        return response.get_json()["strategy"]

    def test_create_defaults_to_paper(self, client):
        strategy = self._create(client)
        assert strategy["execution_mode"] == "PAPER"
        assert strategy["status"] == "STOPPED"
        assert strategy["live_confirmed"] is False

    def test_create_validates_exchange(self, client):
        script_id = client.post(
            "/pine/scripts", json={"name": "A", "code": EMA_CROSS}
        ).get_json()["id"]
        response = client.post(
            "/pine/strategies",
            json={
                "script_id": script_id,
                "name": "X",
                "symbol": "NIFTY",
                "exchange": "NYSE",
                "timeframe": "1m",
            },
        )
        assert response.status_code == 400

    def test_create_validates_timeframe(self, client):
        script_id = client.post(
            "/pine/scripts", json={"name": "A", "code": EMA_CROSS}
        ).get_json()["id"]
        response = client.post(
            "/pine/strategies",
            json={
                "script_id": script_id,
                "name": "X",
                "symbol": "NIFTY",
                "exchange": "NSE",
                "timeframe": "7m",
            },
        )
        assert response.status_code == 400

    def test_live_requires_explicit_confirmation(self, client, monkeypatch):
        strategy = self._create(client)
        instance_id = strategy["id"]

        # Without confirm -> rejected
        response = client.post(f"/pine/strategies/{instance_id}/live", json={})
        assert response.status_code == 400
        assert "confirmation" in response.get_json()["message"]

        # With confirm=false -> rejected
        response = client.post(f"/pine/strategies/{instance_id}/live", json={"confirm": False})
        assert response.status_code == 400

        # With confirm=true -> enabled
        response = client.post(f"/pine/strategies/{instance_id}/live", json={"confirm": True})
        assert response.status_code == 200
        assert response.get_json()["strategy"]["execution_mode"] == "LIVE"

        # Back to paper always allowed
        response = client.post(f"/pine/strategies/{instance_id}/paper", json={})
        assert response.status_code == 200
        assert response.get_json()["strategy"]["execution_mode"] == "PAPER"

    def test_start_stop_flow(self, client, monkeypatch):
        strategy = self._create(client)
        instance_id = strategy["id"]

        # Patch the manager so no real market connection happens.
        started = []
        monkeypatch.setattr(
            pine_blueprint.manager,
            "start",
            lambda instance, code, key: (started.append(instance.id) or (True, "running")),
        )
        monkeypatch.setattr(
            pine_blueprint.manager, "stop", lambda instance: (True, "stopped")
        )
        monkeypatch.setattr(
            pine_blueprint, "_api_key", lambda user_id: "test-key"
        )

        response = client.post(f"/pine/strategies/{instance_id}/start", json={})
        assert response.status_code == 200
        assert started == [instance_id]

        response = client.post(f"/pine/strategies/{instance_id}/stop", json={})
        assert response.status_code == 200

    def test_signals_and_alerts_endpoints(self, client):
        strategy = self._create(client)
        instance_id = strategy["id"]
        pine_db.record_signal(
            signal_id="sig-1",
            instance_id=instance_id,
            idempotency_key="k1",
            signal="BUY",
            kind="entry",
            symbol="NIFTY",
            exchange="NSE",
            timeframe="1m",
            price=100.0,
            quantity=1,
            bar_time=1700000000000,
            bar_index=10,
        )
        signals = client.get(f"/pine/strategies/{instance_id}/signals").get_json()
        assert signals["signals"][0]["signal"] == "BUY"

        orders = client.get(f"/pine/strategies/{instance_id}/orders").get_json()
        assert orders["orders"] == []  # not executed -> not an order yet

        alerts = client.get(f"/pine/strategies/{instance_id}/alerts").get_json()
        assert alerts["alerts"] == []

    def test_delete_stops_first(self, client, monkeypatch):
        strategy = self._create(client)
        instance_id = strategy["id"]
        stopped = []
        monkeypatch.setattr(
            pine_blueprint.manager, "stop", lambda instance: stopped.append(1) or (True, "stopped")
        )
        assert client.delete(f"/pine/strategies/{instance_id}").status_code == 200
        assert stopped == [1]


class TestAuthentication:
    def test_unauthenticated_rejected(self, app, monkeypatch):
        monkeypatch.setattr(session_utils, "is_session_valid", lambda: False)
        with app.test_client() as test_client:
            response = test_client.get(
                "/pine/scripts", headers={"Accept": "application/json"}
            )
            assert response.status_code == 401
