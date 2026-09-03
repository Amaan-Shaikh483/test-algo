"""Database layer for the Pine strategy engine.

Models follow the conventions of ``database/strategy_db.py`` (same main
``openalgo.db`` engine, NullPool, scoped sessions). Tables are created by
``init_db()`` on fresh installs and by ``upgrade/migrate_pine.py`` on existing
ones; the migration is the authoritative path per repository rules.

Entities:
- PineScript           - saved source code (what the editor loads/saves)
- PineStrategyInstance - one running configuration of a script (symbol,
                         timeframe, execution mode, lifecycle status)
- PineSignal           - every emitted signal with its idempotency key; the
                         unique index on (instance_id, idempotency_key) is the
                         duplicate-order guard
- PineAlert            - internal alerts fired by alertcondition()/alert()
- PineBacktestRun      - persisted backtest results for later inspection
"""

import logging
import os
import uuid

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
from sqlalchemy.sql import func

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


class PineScript(Base):
    """A saved Pine script (latest source; versioning via updated_at)."""

    __tablename__ = "pine_scripts"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    code = Column(Text, nullable=False)
    kind = Column(String(20), nullable=False, default="strategy")  # strategy | indicator
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class PineStrategyInstance(Base):
    """A server-side strategy instance: one script bound to symbol/timeframe."""

    __tablename__ = "pine_strategy_instances"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    script_id = Column(Integer, nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    # STOPPED | STARTING | RUNNING | PAUSED | ERROR
    status = Column(String(20), nullable=False, default="STOPPED")
    execution_mode = Column(String(10), nullable=False, default="PAPER")  # PAPER | LIVE
    broker = Column(String(50), nullable=False, default="")
    quantity = Column(Integer, nullable=False, default=1)
    product = Column(String(10), nullable=False, default="MIS")
    inputs = Column(Text, nullable=False, default="{}")  # JSON input overrides
    last_bar_time = Column(Float, nullable=True)
    last_signal_time = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    live_confirmed = Column(Boolean, nullable=False, default=False)


class PineSignal(Base):
    """One strategy signal intent, with the idempotency key that dedupes orders."""

    __tablename__ = "pine_signals"

    id = Column(Integer, primary_key=True)
    signal_id = Column(String(64), unique=True, nullable=False, index=True)
    instance_id = Column(String(36), nullable=False, index=True)
    # Deterministic idempotency: strategy_id + symbol + timeframe + bar_time +
    # signal + sequence. The unique index turns a duplicate replay (reconnect,
    # restart, browser refresh) into an IntegrityError instead of a duplicate
    # broker order.
    idempotency_key = Column(String(180), nullable=False, index=True, unique=True)
    signal = Column(String(10), nullable=False)  # BUY | SELL
    kind = Column(String(20), nullable=False)  # entry | close | exit_stop | exit_limit
    order_ref = Column(String(100), nullable=False, default="")
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    price = Column(Float, nullable=False, default=0.0)
    quantity = Column(Float, nullable=False, default=0.0)
    bar_time = Column(Float, nullable=False, default=0.0)
    bar_index = Column(Integer, nullable=False, default=0)
    source = Column(String(20), nullable=False, default="realtime")  # realtime | historical
    executed = Column(Boolean, nullable=False, default=False)
    order_id = Column(String(100), nullable=True)
    order_status = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PineAlert(Base):
    """An internal alert fired by the Pine runtime."""

    __tablename__ = "pine_alerts"

    id = Column(Integer, primary_key=True)
    instance_id = Column(String(36), nullable=False, index=True)
    kind = Column(String(20), nullable=False)  # condition | call
    title = Column(String(255), nullable=False, default="")
    message = Column(Text, nullable=False, default="")
    symbol = Column(String(50), nullable=False, default="")
    exchange = Column(String(20), nullable=False, default="")
    timeframe = Column(String(10), nullable=False, default="")
    bar_time = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PineBacktestRun(Base):
    """A persisted backtest result."""

    __tablename__ = "pine_backtest_runs"

    id = Column(Integer, primary_key=True)
    script_id = Column(Integer, nullable=False, index=True)
    user_id = Column(String(255), nullable=False)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    params = Column(Text, nullable=False, default="{}")
    metrics = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def init_db() -> None:
    """Create the Pine tables on a fresh installation."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Pine DB", logger)


# ---------------------------------------------------------------------------
# Script CRUD
# ---------------------------------------------------------------------------


def create_script(user_id: str, name: str, code: str, kind: str) -> PineScript | None:
    try:
        script = PineScript(user_id=user_id, name=name, code=code, kind=kind)
        db_session.add(script)
        db_session.commit()
        return script
    except Exception:
        db_session.rollback()
        logger.exception("Failed to create Pine script")
        return None


def update_script(script_id: int, code: str | None = None, name: str | None = None) -> bool:
    try:
        script = db_session.query(PineScript).filter(PineScript.id == script_id).first()
        if script is None:
            return False
        if code is not None:
            script.code = code
        if name is not None:
            script.name = name
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("Failed to update Pine script %s", script_id)
        return False


def get_script(script_id: int) -> PineScript | None:
    try:
        return db_session.query(PineScript).filter(PineScript.id == script_id).first()
    except Exception:
        db_session.rollback()
        logger.exception("Failed to fetch Pine script %s", script_id)
        return None


def get_user_scripts(user_id: str) -> list[PineScript]:
    try:
        return (
            db_session.query(PineScript)
            .filter(PineScript.user_id == user_id)
            .order_by(PineScript.updated_at.desc())
            .all()
        )
    except Exception:
        db_session.rollback()
        logger.exception("Failed to list Pine scripts for %s", user_id)
        return []


def delete_script(script_id: int) -> bool:
    try:
        script = db_session.query(PineScript).filter(PineScript.id == script_id).first()
        if script is None:
            return False
        db_session.delete(script)
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("Failed to delete Pine script %s", script_id)
        return False


# ---------------------------------------------------------------------------
# Strategy instances
# ---------------------------------------------------------------------------


def create_instance(**kwargs) -> PineStrategyInstance | None:
    try:
        instance = PineStrategyInstance(**kwargs)
        db_session.add(instance)
        db_session.commit()
        return instance
    except Exception:
        db_session.rollback()
        logger.exception("Failed to create Pine strategy instance")
        return None


def get_instance(instance_id: str) -> PineStrategyInstance | None:
    try:
        return (
            db_session.query(PineStrategyInstance)
            .filter(PineStrategyInstance.id == instance_id)
            .first()
        )
    except Exception:
        db_session.rollback()
        logger.exception("Failed to fetch Pine strategy instance %s", instance_id)
        return None


def get_user_instances(user_id: str) -> list[PineStrategyInstance]:
    try:
        return (
            db_session.query(PineStrategyInstance)
            .filter(PineStrategyInstance.user_id == user_id)
            .order_by(PineStrategyInstance.created_at.desc())
            .all()
        )
    except Exception:
        db_session.rollback()
        logger.exception("Failed to list Pine strategy instances for %s", user_id)
        return []


def get_active_instances() -> list[PineStrategyInstance]:
    """Instances to restore on boot: anything not explicitly stopped."""
    try:
        return (
            db_session.query(PineStrategyInstance)
            .filter(PineStrategyInstance.status.in_(["RUNNING", "STARTING", "PAUSED", "ERROR"]))
            .all()
        )
    except Exception:
        db_session.rollback()
        logger.exception("Failed to list active Pine strategy instances")
        return []


def update_instance(instance_id: str, **kwargs) -> bool:
    try:
        db_session.query(PineStrategyInstance).filter(
            PineStrategyInstance.id == instance_id
        ).update(kwargs)
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("Failed to update Pine strategy instance %s", instance_id)
        return False


def delete_instance(instance_id: str) -> bool:
    try:
        instance = (
            db_session.query(PineStrategyInstance)
            .filter(PineStrategyInstance.id == instance_id)
            .first()
        )
        if instance is None:
            return False
        db_session.delete(instance)
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("Failed to delete Pine strategy instance %s", instance_id)
        return False


# ---------------------------------------------------------------------------
# Signals / alerts (idempotency lives here)
# ---------------------------------------------------------------------------


def signal_key_exists(idempotency_key: str) -> bool:
    try:
        row = (
            db_session.query(PineSignal.id)
            .filter(PineSignal.idempotency_key == idempotency_key)
            .first()
        )
        return row is not None
    except Exception:
        db_session.rollback()
        logger.exception("Failed to check Pine signal key %s", idempotency_key)
        return False


def record_signal(**kwargs) -> PineSignal | None:
    """Insert a signal row; returns None when the idempotency key already exists."""
    try:
        signal = PineSignal(**kwargs)
        db_session.add(signal)
        db_session.commit()
        return signal
    except Exception:
        db_session.rollback()
        # IntegrityError on the unique key is the expected duplicate path.
        return None


def mark_signal_executed(signal_id: str, order_id: str, order_status: str) -> bool:
    try:
        db_session.query(PineSignal).filter(PineSignal.signal_id == signal_id).update(
            {"executed": True, "order_id": order_id, "order_status": order_status}
        )
        db_session.commit()
        return True
    except Exception:
        db_session.rollback()
        logger.exception("Failed to mark Pine signal %s executed", signal_id)
        return False


def get_instance_signals(instance_id: str, limit: int = 200) -> list[PineSignal]:
    try:
        return (
            db_session.query(PineSignal)
            .filter(PineSignal.instance_id == instance_id)
            .order_by(PineSignal.id.desc())
            .limit(limit)
            .all()
        )
    except Exception:
        db_session.rollback()
        logger.exception("Failed to list signals for instance %s", instance_id)
        return []


def record_alert(**kwargs) -> PineAlert | None:
    try:
        alert = PineAlert(**kwargs)
        db_session.add(alert)
        db_session.commit()
        return alert
    except Exception:
        db_session.rollback()
        logger.exception("Failed to record Pine alert")
        return None


def get_instance_alerts(instance_id: str, limit: int = 200) -> list[PineAlert]:
    try:
        return (
            db_session.query(PineAlert)
            .filter(PineAlert.instance_id == instance_id)
            .order_by(PineAlert.id.desc())
            .limit(limit)
            .all()
        )
    except Exception:
        db_session.rollback()
        logger.exception("Failed to list alerts for instance %s", instance_id)
        return []


# ---------------------------------------------------------------------------
# Backtests
# ---------------------------------------------------------------------------


def record_backtest(**kwargs) -> PineBacktestRun | None:
    try:
        run = PineBacktestRun(**kwargs)
        db_session.add(run)
        db_session.commit()
        return run
    except Exception:
        db_session.rollback()
        logger.exception("Failed to record Pine backtest")
        return None

