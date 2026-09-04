"""Tests for upgrade/migrate_alerts.py.

Verifies the migration is idempotent, creates the three alert tables with
the unique idempotency-key index, and is safe on an already-populated
database (existing pine rows untouched, alert rows preserved on re-run).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "upgrade" / "migrate_alerts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("migrate_alerts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(db_url):
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "upgrade"),
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": db_url},
        timeout=60,
    )
    return result


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'alerts-migration.db'}"


class TestMigration:
    def test_creates_tables_and_indexes(self, db_url):
        result = _run_script(db_url)
        assert result.returncode == 0, result.stderr
        engine = create_engine(db_url)
        tables = set(inspect(engine).get_table_names())
        assert {"alerts", "alert_events", "alert_deliveries"} <= tables
        indexes = {
            i["name"]: i for i in inspect(engine).get_indexes("alert_events")
        }
        assert indexes["ix_alert_events_idempotency_key"]["unique"] in (True, 1)
        engine.dispose()

    def test_idempotent_rerun(self, db_url):
        first = _run_script(db_url)
        assert first.returncode == 0, first.stderr
        engine = create_engine(db_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO alerts (id, user_id, name, symbol, exchange, timeframe,"
                    " source_type, webhook_url, status, enabled)"
                    " VALUES ('a1', 'u1', 'keep', 'NIFTY', 'NSE', '5m', 'price',"
                    " 'https://example.com/h', 'ACTIVE', 1)"
                )
            )
        engine.dispose()
        second = _run_script(db_url)
        assert second.returncode == 0, second.stderr
        assert "nothing to do" in second.stdout
        engine = create_engine(db_url)
        with engine.begin() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM alerts")).scalar()
        assert count == 1  # existing rows untouched by the re-run
        engine.dispose()

    def test_safe_on_populated_database(self, db_url):
        """A populated app database (pine tables + rows) survives the migration."""
        engine = create_engine(db_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE pine_scripts (id INTEGER PRIMARY KEY, user_id TEXT,"
                    " name TEXT, code TEXT)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO pine_scripts (user_id, name, code)"
                    " VALUES ('u1', 'EMA', 'strategy(\"EMA\")')"
                )
            )
        engine.dispose()
        result = _run_script(db_url)
        assert result.returncode == 0, result.stderr
        engine = create_engine(db_url)
        with engine.begin() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM pine_scripts")).scalar() == 1
        tables = set(inspect(engine).get_table_names())
        assert {"pine_scripts", "alerts", "alert_events", "alert_deliveries"} <= tables
        engine.dispose()

    def test_registered_in_migrate_all(self):
        """The migration must be registered in the master MIGRATIONS list."""
        content = (REPO_ROOT / "upgrade" / "migrate_all.py").read_text()
        assert '("migrate_alerts.py", "Alert + Webhook System")' in content

    def test_status_flag(self, db_url):
        assert _run_script(db_url).returncode == 0
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--status"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT / "upgrade"),
            env={"PATH": "/usr/bin:/bin", "DATABASE_URL": db_url},
            timeout=60,
        )
        assert result.returncode == 0
        assert "EXISTS" in result.stdout
