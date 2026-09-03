#!/usr/bin/env python
"""
Pine Strategy Engine Migration Script for OpenAlgo

Adds five tables to the main openalgo database:

- ``pine_scripts``            — saved Pine source code
- ``pine_strategy_instances`` — server-side strategy instances (lifecycle state)
- ``pine_signals``            — every emitted signal with its idempotency key
- ``pine_alerts``             — internal alerts fired by the Pine runtime
- ``pine_backtest_runs``      — persisted backtest results

Idempotent — safe to run multiple times. Uses CREATE TABLE IF NOT EXISTS so a
re-run never touches existing rows, and the unique index on
``pine_signals.idempotency_key`` is created only when missing.

Usage:
    cd upgrade
    uv run migrate_pine.py           # Apply migration
    uv run migrate_pine.py --status  # Check status

Migration: pine-strategy-engine
Created: 2026-09-02
"""

import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Register the app's SQLite pragmas on this process's engines, so a migration
# waits the same 15s for a write lock the running app does instead of the
# sqlite3 default of 5s (GitHub issue #1726).
import _pragmas  # noqa: F401,E402
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "pine_strategy_engine"
MIGRATION_VERSION = "pine-engine"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = {
    "pine_scripts": """
        CREATE TABLE IF NOT EXISTS pine_scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            code TEXT NOT NULL,
            kind VARCHAR(20) NOT NULL DEFAULT 'strategy',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "pine_strategy_instances": """
        CREATE TABLE IF NOT EXISTS pine_strategy_instances (
            id VARCHAR(36) PRIMARY KEY,
            script_id INTEGER NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'STOPPED',
            execution_mode VARCHAR(10) NOT NULL DEFAULT 'PAPER',
            broker VARCHAR(50) NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1,
            product VARCHAR(10) NOT NULL DEFAULT 'MIS',
            inputs TEXT NOT NULL DEFAULT '{}',
            last_bar_time FLOAT,
            last_signal_time DATETIME,
            last_error TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            live_confirmed BOOLEAN NOT NULL DEFAULT 0
        )
    """,
    "pine_signals": """
        CREATE TABLE IF NOT EXISTS pine_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id VARCHAR(64) NOT NULL,
            instance_id VARCHAR(36) NOT NULL,
            idempotency_key VARCHAR(180) NOT NULL,
            signal VARCHAR(10) NOT NULL,
            kind VARCHAR(20) NOT NULL,
            order_ref VARCHAR(100) NOT NULL DEFAULT '',
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            price FLOAT NOT NULL DEFAULT 0,
            quantity FLOAT NOT NULL DEFAULT 0,
            bar_time FLOAT NOT NULL DEFAULT 0,
            bar_index INTEGER NOT NULL DEFAULT 0,
            source VARCHAR(20) NOT NULL DEFAULT 'realtime',
            executed BOOLEAN NOT NULL DEFAULT 0,
            order_id VARCHAR(100),
            order_status VARCHAR(50),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "pine_alerts": """
        CREATE TABLE IF NOT EXISTS pine_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id VARCHAR(36) NOT NULL,
            kind VARCHAR(20) NOT NULL,
            title VARCHAR(255) NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            symbol VARCHAR(50) NOT NULL DEFAULT '',
            exchange VARCHAR(20) NOT NULL DEFAULT '',
            timeframe VARCHAR(10) NOT NULL DEFAULT '',
            bar_time FLOAT NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "pine_backtest_runs": """
        CREATE TABLE IF NOT EXISTS pine_backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id INTEGER NOT NULL,
            user_id VARCHAR(255) NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(20) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            metrics TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """,
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_pine_scripts_user ON pine_scripts (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_pine_instances_user ON pine_strategy_instances (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_pine_instances_script ON pine_strategy_instances (script_id)",
    "CREATE INDEX IF NOT EXISTS ix_pine_signals_instance ON pine_signals (instance_id)",
    "CREATE INDEX IF NOT EXISTS ix_pine_signals_signal_id ON pine_signals (signal_id)",
    # The duplicate-order guard: one signal row per (instance, bar, order).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pine_signals_idem ON pine_signals (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS ix_pine_alerts_instance ON pine_alerts (instance_id)",
    "CREATE INDEX IF NOT EXISTS ix_pine_backtest_script ON pine_backtest_runs (script_id)",
]


def get_main_db_engine():
    """Get the main openalgo database engine."""
    db_url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.join(parent_dir, db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db_url = f"sqlite:///{db_path}"
        logger.info(f"Main DB path: {db_path}")

    return create_engine(db_url)


def table_exists(conn, name: str) -> bool:
    result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name})
    return result.fetchone() is not None


def index_exists(conn, name: str) -> bool:
    result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name})
    return result.fetchone() is not None


def apply_migration(engine) -> bool:
    """Create the Pine tables and indexes. Returns True when work was done."""
    changed = False
    with engine.connect() as conn:
        for table_name, ddl in TABLES.items():
            if table_exists(conn, table_name):
                logger.info(f"Table {table_name} already exists, skipping")
                continue
            logger.info(f"Creating table {table_name}")
            conn.execute(text(ddl))
            changed = True

        for index_ddl in INDEXES:
            index_name = index_ddl.split("IF NOT EXISTS ")[1].split(" ")[0]
            if index_exists(conn, index_name):
                continue
            logger.info(f"Creating index {index_name}")
            conn.execute(text(index_ddl))
            changed = True

        conn.commit()
    return changed


def check_status(engine) -> bool:
    """Report what would change without changing it."""
    pending = False
    with engine.connect() as conn:
        for table_name in TABLES:
            present = table_exists(conn, table_name)
            if not present:
                pending = True
            logger.info(f"Table {table_name}: {'present' if present else 'MISSING'}")
        for index_ddl in INDEXES:
            index_name = index_ddl.split("IF NOT EXISTS ")[1].split(" ")[0]
            present = index_exists(conn, index_name)
            if not present:
                pending = True
            logger.info(f"Index {index_name}: {'present' if present else 'MISSING'}")
    return pending


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{MIGRATION_NAME} migration")
    parser.add_argument("--status", action="store_true", help="Check status without applying")
    args = parser.parse_args()

    engine = get_main_db_engine()

    if args.status:
        pending = check_status(engine)
        logger.info(f"Migration {MIGRATION_VERSION}: {'PENDING' if pending else 'COMPLETE'}")
        return 0

    logger.info(f"Running migration: {MIGRATION_NAME} ({MIGRATION_VERSION})")
    changed = apply_migration(engine)
    logger.info("Migration complete" + (" (changes applied)" if changed else " (already up to date)"))

    # Sanity: verify the schema is present afterwards.
    inspector = inspect(engine)
    for table_name in TABLES:
        if table_name not in inspector.get_table_names():
            logger.error(f"Table {table_name} missing after migration")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
