#!/usr/bin/env python
"""
Alert + Webhook System Migration Script for OpenAlgo

Adds three tables to the main openalgo database:

- ``alerts``           — user alert definitions (price/strategy conditions)
- ``alert_events``     — every logical trigger with its unique idempotency key
- ``alert_deliveries`` — webhook delivery attempts with HTTP status

Idempotent — safe to run multiple times. Uses CREATE TABLE IF NOT EXISTS so a
re-run never touches existing rows, and the unique index on
``alert_events.idempotency_key`` is created only when missing.

Usage:
    cd upgrade
    uv run migrate_alerts.py           # Apply migration
    uv run migrate_alerts.py --status  # Check status

Migration: alert-webhook-system
Created: 2026-09-04
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

MIGRATION_NAME = "alert_webhook_system"
MIGRATION_VERSION = "alerts-v1"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))

TABLES = {
    "alerts": """
        CREATE TABLE IF NOT EXISTS alerts (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(50) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            source_type VARCHAR(20) NOT NULL,
            strategy_id VARCHAR(64),
            signal VARCHAR(20),
            operator VARCHAR(30),
            value FLOAT,
            trigger_mode VARCHAR(20) NOT NULL DEFAULT 'once_only',
            expiration DATETIME,
            message TEXT,
            webhook_url TEXT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME,
            last_triggered_at DATETIME
        )
    """,
    "alert_events": """
        CREATE TABLE IF NOT EXISTS alert_events (
            id VARCHAR(36) PRIMARY KEY,
            alert_id VARCHAR(36) NOT NULL,
            event_type VARCHAR(30) NOT NULL,
            signal VARCHAR(20),
            symbol VARCHAR(50) NOT NULL,
            price FLOAT,
            bar_time FLOAT,
            idempotency_key VARCHAR(64) NOT NULL,
            payload TEXT,
            created_at DATETIME
        )
    """,
    "alert_deliveries": """
        CREATE TABLE IF NOT EXISTS alert_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_event_id VARCHAR(36) NOT NULL,
            alert_id VARCHAR(36) NOT NULL,
            webhook_url TEXT NOT NULL,
            status VARCHAR(12) NOT NULL DEFAULT 'PENDING',
            attempt INTEGER NOT NULL DEFAULT 0,
            http_status INTEGER,
            error TEXT,
            created_at DATETIME,
            completed_at DATETIME
        )
    """,
}

INDEXES = {
    "ix_alerts_user_id": "CREATE INDEX IF NOT EXISTS ix_alerts_user_id ON alerts (user_id)",
    "ix_alert_events_alert_id": (
        "CREATE INDEX IF NOT EXISTS ix_alert_events_alert_id ON alert_events (alert_id)"
    ),
    "ix_alert_events_idempotency_key": (
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_alert_events_idempotency_key "
        "ON alert_events (idempotency_key)"
    ),
    "ix_alert_deliveries_alert_event_id": (
        "CREATE INDEX IF NOT EXISTS ix_alert_deliveries_alert_event_id "
        "ON alert_deliveries (alert_event_id)"
    ),
    "ix_alert_deliveries_alert_id": (
        "CREATE INDEX IF NOT EXISTS ix_alert_deliveries_alert_id ON alert_deliveries (alert_id)"
    ),
}


def get_engine():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)
    if "sqlite" in database_url:
        return create_engine(
            database_url, connect_args={"check_same_thread": False}
        )
    return create_engine(database_url)


def check_status():
    engine = get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    print(f"\n{'=' * 50}")
    print("Alert Webhook System Migration Status")
    print(f"{'=' * 50}")
    for table in TABLES:
        status = "EXISTS" if table in existing else "MISSING"
        print(f"  {table:20s} {status}")
    idx_names = set()
    for table in TABLES:
        if table in existing:
            idx_names.update(i["name"] for i in inspector.get_indexes(table))
    for idx in INDEXES:
        status = "EXISTS" if idx in idx_names else "MISSING"
        print(f"  {idx:32s} {status}")
    engine.dispose()


def run_migration():
    engine = get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    needed = [t for t in TABLES if t not in existing]
    if not needed:
        idx_names = set()
        for table in TABLES:
            idx_names.update(i["name"] for i in inspector.get_indexes(table))
        missing_idx = [i for i in INDEXES if i not in idx_names]
        if not missing_idx:
            print("Alert tables already exist - nothing to do (idempotent)")
            engine.dispose()
            return
        needed = []

    print(f"Applying alert webhook system migration ({MIGRATION_VERSION})...")
    with engine.begin() as conn:
        for table in needed:
            print(f"  Creating table {table}...")
            conn.execute(text(TABLES[table]))
        for name, ddl in INDEXES.items():
            try:
                conn.execute(text(ddl))
                print(f"  Index {name}: ensured")
            except Exception as exc:
                print(f"  Index {name}: skipped ({exc})")

    print("Alert webhook system migration complete")
    engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Alert + Webhook system migration")
    parser.add_argument("--status", action="store_true", help="Check migration status")
    args = parser.parse_args()
    if args.status:
        check_status()
    else:
        run_migration()


if __name__ == "__main__":
    main()
