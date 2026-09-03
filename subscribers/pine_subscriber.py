"""SocketIO subscriber for Pine strategy events.

Emits the ``strategy_*`` realtime events the /trading Pine panel listens to.
Called from the event bus thread pool - socketio.emit() is thread-safe with
async_mode="threading" and avoids greenlet errors under eventlet, matching the
pattern of socketio_subscriber.py.
"""

from extensions import socketio
from utils.logging import get_logger

logger = get_logger(__name__)


def on_pine_signal(event) -> None:
    """Emit strategy_signal: a BUY/SELL intent produced by the runtime."""
    socketio.emit(
        "strategy_signal",
        {
            "strategy_id": event.strategy_id,
            "strategy_name": event.strategy_name,
            "signal_id": event.signal_id,
            "signal": event.signal,
            "kind": event.kind,
            "symbol": event.symbol,
            "exchange": event.exchange,
            "timeframe": event.timeframe,
            "price": event.price,
            "quantity": event.quantity,
            "bar_time": event.bar_time,
            "mode": event.mode,
            "source": event.source,
            "timestamp": event.request_data.get("timestamp") if event.request_data else None,
        },
    )


def on_pine_alert(event) -> None:
    """Emit strategy_alert: an internal alert fired by the Pine script."""
    socketio.emit(
        "strategy_alert",
        {
            "strategy_id": event.strategy_id,
            "strategy_name": event.strategy_name,
            "kind": event.alert_kind,
            "title": event.title,
            "message": event.message,
            "symbol": event.symbol,
            "exchange": event.exchange,
            "timeframe": event.timeframe,
            "bar_time": event.bar_time,
        },
    )


def on_pine_status(event) -> None:
    """Emit strategy_status: lifecycle transitions of a strategy instance."""
    socketio.emit(
        "strategy_status",
        {
            "strategy_id": event.strategy_id,
            "strategy_name": event.strategy_name,
            "status": event.status,
            "detail": event.detail,
            "symbol": event.symbol,
            "exchange": event.exchange,
            "timeframe": event.timeframe,
            "mode": event.mode,
        },
    )


def on_pine_order(event) -> None:
    """Emit strategy_order: result of routing a signal into the order pipeline."""
    socketio.emit(
        "strategy_order",
        {
            "strategy_id": event.strategy_id,
            "strategy_name": event.strategy_name,
            "signal_id": event.signal_id,
            "signal": event.signal,
            "order_id": event.order_id,
            "order_status": event.order_status,
            "message": event.message,
            "mode": event.mode,
            "symbol": event.symbol,
            "exchange": event.exchange,
        },
    )


def on_pine_error(event) -> None:
    """Emit strategy_error: a running strategy hit a runtime failure."""
    socketio.emit(
        "strategy_error",
        {
            "strategy_id": event.strategy_id,
            "strategy_name": event.strategy_name,
            "error": event.error,
            "status": event.status or "ERROR",
        },
    )
