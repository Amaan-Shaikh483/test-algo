"""Database layer for the Alert + Webhook system.

Models follow the conventions of ``database/pine_db.py`` (same main
``openalgo.db`` engine, NullPool, scoped sessions). Tables are created by
``init_db()`` on fresh installs and by ``upgrade/migrate_alerts.py`` on
existing ones; the migration is the authoritative path per repository rules.

Entities:
- Alert         - one user alert (price condition or strategy-signal condition)
- AlertEvent    - every logical trigger; unique ``idempotency_key`` is the
                  duplicate-delivery guard (retries never create new events)
- AlertDelivery - per-event webhook delivery attempts with HTTP status
"""

import logging
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(
        DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10
    ) if DATABASE_URL else None

if engine is not None:
    db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
else:  # pragma: no cover - only hit when imported without DATABASE_URL (tests)
    db_session = scoped_session(sessionmaker())

Base = declarative_base()
Base.query = db_session.query_property()


class Alert(Base):
    """One user alert. ``status`` lifecycle: ACTIVE -> TRIGGERED / EXPIRED."""

    __tablename__ = "alerts"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(50), nullable=False)
    timeframe = Column(String(10), nullable=False)
    # price | strategy
    source_type = Column(String(20), nullable=False)
    # strategy-source alerts: which instance + which signal(s) to match
    strategy_id = Column(String(64), nullable=True)
    signal = Column(String(20), nullable=True)  # BUY | SELL | ANY
    # price-source alerts: operator + target value
    operator = Column(String(30), nullable=True)
    value = Column(Float, nullable=True)
    # once_only for MVP; every_time / once_per_bar / once_per_bar_close reserved
    trigger_mode = Column(String(20), nullable=False, default="once_only")
    expiration = Column(DateTime, nullable=True)
    message = Column(Text, nullable=True)
    webhook_url = Column(Text, nullable=False)
    # ACTIVE | TRIGGERED | EXPIRED
    status = Column(String(20), nullable=False, default="ACTIVE")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=None)
    updated_at = Column(DateTime, default=None)
    last_triggered_at = Column(DateTime, nullable=True)


class AlertEvent(Base):
    """One logical alert trigger (deduplicated via idempotency_key)."""

    __tablename__ = "alert_events"

    id = Column(String(36), primary_key=True)
    alert_id = Column(String(36), nullable=False, index=True)
    # strategy_signal | price_cross
    event_type = Column(String(30), nullable=False)
    signal = Column(String(20), nullable=True)
    symbol = Column(String(50), nullable=False)
    price = Column(Float, nullable=True)
    bar_time = Column(Float, nullable=True)  # ms epoch of the triggering bar/tick
    idempotency_key = Column(String(64), nullable=False, unique=True, index=True)
    payload = Column(Text, nullable=True)  # JSON snapshot actually delivered
    created_at = Column(DateTime, default=None)


class AlertDelivery(Base):
    """Webhook delivery attempts for one alert event."""

    __tablename__ = "alert_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_event_id = Column(String(36), nullable=False, index=True)
    alert_id = Column(String(36), nullable=False, index=True)
    webhook_url = Column(Text, nullable=False)
    # PENDING | SENDING | SUCCESS | RETRYING | FAILED
    status = Column(String(12), nullable=False, default="PENDING")
    attempt = Column(Integer, nullable=False, default=0)
    http_status = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=None)
    completed_at = Column(DateTime, nullable=True)


def init_db() -> bool:
    """Create alert tables if missing. The migration is the authoritative path."""
    if engine is None:
        return False
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if {"alerts", "alert_events", "alert_deliveries"} <= existing:
        return True
    Base.metadata.create_all(bind=engine)
    return True
