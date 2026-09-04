"""SocketIO subscriber for Alert + Webhook system events.

Emits the ``alert_triggered`` / ``alert_delivery`` realtime events the
/trading alert panel listens to. Called from the event bus thread pool —
socketio.emit() is thread-safe with async_mode="threading" and avoids
greenlet errors under eventlet, matching the pattern of pine_subscriber.py.
"""

from extensions import socketio
from utils.logging import get_logger

logger = get_logger(__name__)


def on_alert_triggered(event) -> None:
    """Emit alert_triggered: an alert matched and its event was recorded."""
    socketio.emit(
        "alert_triggered",
        {
            "alert_id": event.alert_id,
            "user_id": event.user_id,
            "symbol": event.symbol,
            "event_type": event.event_type,
            "signal": event.signal,
            "price": event.price,
            "message": event.message,
        },
    )


def on_alert_delivery(event) -> None:
    """Emit alert_delivery: a webhook delivery attempt finished."""
    socketio.emit(
        "alert_delivery",
        {
            "alert_id": event.alert_id,
            "event_id": event.event_id,
            "status": event.status,
            "http_status": event.http_status,
            "error": event.error,
        },
    )
