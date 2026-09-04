"""HTTP API for the Alert + Webhook system on the /trading terminal.

Session-authenticated JSON endpoints following the pine blueprint
conventions. Alerts are evaluated server-side by ``services.alert_engine``
(the browser/editor being closed never stops them).

Routes:
    GET    /alerts                      - list the user's alerts
    POST   /alerts                      - create an alert
    GET    /alerts/<id>                 - alert detail
    PUT    /alerts/<id>                 - update an alert
    DELETE /alerts/<id>                 - delete an alert (and stop it)
    POST   /alerts/<id>/enable          - re-enable a disabled alert
    POST   /alerts/<id>/disable         - pause evaluation
    GET    /alerts/<id>/logs            - events + webhook delivery history
    POST   /alerts/test                 - send a test webhook (never orders)
"""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from database.alert_db import Alert, AlertDelivery, AlertEvent, db_session
from database.auth_db import get_api_key_for_tradingview
from limiter import limiter
from services.alert_engine import (
    PRICE_OPERATORS,
    STRATEGY_SIGNALS,
    alert_engine,
    validate_webhook_url,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

alerts_bp = Blueprint("alerts_bp", __name__, url_prefix="/alerts")

VALID_TIMEFRAMES = {
    "1s", "5s", "10s", "15s", "30s",
    "1m", "3m", "5m", "10m", "15m", "30m",
    "1h", "2h", "3h", "4h", "D", "W", "M",
}

VALID_EXCHANGES = {
    "NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX", "NCDEX",
    "NSE_INDEX", "BSE_INDEX", "MCX_INDEX", "GLOBAL_INDEX",
}


def _user_id() -> str | None:
    return session.get("user")


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


def _serialize_alert(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "name": alert.name,
        "symbol": alert.symbol,
        "exchange": alert.exchange,
        "timeframe": alert.timeframe,
        "source_type": alert.source_type,
        "strategy_id": alert.strategy_id,
        "signal": alert.signal,
        "operator": alert.operator,
        "value": alert.value,
        "trigger_mode": alert.trigger_mode,
        "expiration": alert.expiration.isoformat() if alert.expiration else None,
        "message": alert.message,
        "webhook_url": alert.webhook_url,
        "status": alert.status,
        "enabled": bool(alert.enabled),
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "last_triggered_at": (
            alert.last_triggered_at.isoformat() if alert.last_triggered_at else None
        ),
    }


def _get_owned_alert(alert_id: str) -> Alert | None:
    """Fetch an alert owned by the session user, or None (404/403 handled)."""
    user_id = _user_id()
    if not user_id:
        return None
    alert = db_session.query(Alert).filter(Alert.id == alert_id).first()
    if alert is None or alert.user_id != user_id:
        return None
    return alert


def _validate_payload(data: dict, partial: bool = False, current: Alert | None = None) -> tuple[dict, str | None]:
    """Validate create/update payload; returns (clean, error).

    ``current`` is the existing alert on partial updates, so a value-only PUT
    is validated against the alert's stored source_type.
    """
    clean: dict = {}
    if "symbol" in data or not partial:
        symbol = str(data.get("symbol") or "").strip().upper()
        if not symbol:
            return clean, "Symbol is required"
        clean["symbol"] = symbol
    if "exchange" in data or not partial:
        exchange = str(data.get("exchange") or "").strip().upper()
        if exchange not in VALID_EXCHANGES:
            return clean, f"Unsupported exchange: {exchange}"
        clean["exchange"] = exchange
    if "timeframe" in data or not partial:
        timeframe = str(data.get("timeframe") or "").strip()
        if timeframe not in VALID_TIMEFRAMES:
            return clean, f"Unsupported timeframe: {timeframe}"
        clean["timeframe"] = timeframe
    if "source_type" in data or not partial:
        source_type = str(data.get("source_type") or "").strip()
        if source_type not in ("price", "strategy"):
            return clean, "source_type must be 'price' or 'strategy'"
        clean["source_type"] = source_type

    effective_source = clean.get("source_type") or (
        current.source_type if current is not None else None
    )
    if effective_source == "price" and ("operator" in data or "value" in data or not partial):
        if "operator" in data or not partial:
            operator = str(data.get("operator") or "").strip()
            if operator not in PRICE_OPERATORS:
                return clean, f"Unsupported operator: {operator}"
            clean["operator"] = operator
        if "value" in data or not partial:
            try:
                clean["value"] = float(data.get("value"))
            except (TypeError, ValueError):
                return clean, "Value must be a number"

    if effective_source == "strategy" and ("signal" in data or "strategy_id" in data or not partial):
        if "signal" in data or not partial:
            signal = str(data.get("signal") or "ANY").strip().upper()
            if signal not in STRATEGY_SIGNALS:
                return clean, "signal must be BUY, SELL or ANY"
            clean["signal"] = signal
        if "strategy_id" in data or not partial:
            strategy_id = str(data.get("strategy_id") or "").strip()
            if not strategy_id:
                return clean, "strategy_id is required for strategy alerts"
            clean["strategy_id"] = strategy_id

    if "webhook_url" in data or not partial:
        url = str(data.get("webhook_url") or "").strip()
        ok, reason = validate_webhook_url(url)
        if not ok:
            return clean, reason
        clean["webhook_url"] = url
    if "message" in data:
        clean["message"] = str(data.get("message") or "").strip()[:500] or None
    if "name" in data:
        clean["name"] = str(data.get("name") or "").strip()[:255]
    if "expiration" in data:
        raw = data.get("expiration")
        if raw in (None, "", "none"):
            clean["expiration"] = None
        else:
            try:
                clean["expiration"] = datetime.fromisoformat(str(raw))
            except ValueError:
                return clean, "expiration must be an ISO-8601 datetime"
    if "trigger_mode" in data or not partial:
        trigger_mode = str(data.get("trigger_mode") or "once_only").strip()
        if trigger_mode != "once_only":
            return clean, "Only 'once_only' trigger mode is supported"
        clean["trigger_mode"] = trigger_mode
    return clean, None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@alerts_bp.route("", methods=["GET"])
@check_session_validity
def list_alerts():
    try:
        user_id = _user_id()
        alerts = (
            db_session.query(Alert)
            .filter(Alert.user_id == user_id)
            .order_by(Alert.created_at.desc())
            .all()
        )
        return jsonify({"status": "success", "alerts": [_serialize_alert(a) for a in alerts]})
    except Exception:
        logger.exception("Failed to list alerts")
        return jsonify({"status": "error", "message": "Failed to list alerts"}), 500


@alerts_bp.route("", methods=["POST"])
@check_session_validity
@limiter.limit("10 per minute")
def create_alert():
    try:
        data = _json_body()
        clean, error = _validate_payload(data)
        if error:
            return jsonify({"status": "error", "message": error}), 400
        user_id = _user_id()
        name = clean.get("name") or (
            f"{clean['symbol']} "
            + (
                f"{clean['signal']} signal"
                if clean["source_type"] == "strategy"
                else f"{clean['operator'].replace('_', ' ')} {clean.get('value', '')}"
            )
        )
        alert = Alert(
            id=uuid.uuid4().hex,
            user_id=user_id,
            name=name,
            symbol=clean["symbol"],
            exchange=clean["exchange"],
            timeframe=clean["timeframe"],
            source_type=clean["source_type"],
            strategy_id=clean.get("strategy_id"),
            signal=clean.get("signal"),
            operator=clean.get("operator"),
            value=clean.get("value"),
            trigger_mode=clean.get("trigger_mode", "once_only"),
            expiration=clean.get("expiration"),
            message=clean.get("message"),
            webhook_url=clean["webhook_url"],
            status="ACTIVE",
            enabled=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db_session.add(alert)
        db_session.commit()
        api_key = get_api_key_for_tradingview(user_id)
        if api_key:
            alert_engine.register_alert(alert.id, user_id, api_key)
        return jsonify({"status": "success", "alert": _serialize_alert(alert)}), 201
    except Exception:
        logger.exception("Failed to create alert")
        db_session.rollback()
        return jsonify({"status": "error", "message": "Failed to create alert"}), 500


@alerts_bp.route("/<alert_id>", methods=["GET"])
@check_session_validity
def get_alert(alert_id):
    try:
        alert = _get_owned_alert(alert_id)
        if alert is None:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        return jsonify({"status": "success", "alert": _serialize_alert(alert)})
    except Exception:
        logger.exception("Failed to fetch alert")
        return jsonify({"status": "error", "message": "Failed to fetch alert"}), 500


@alerts_bp.route("/<alert_id>", methods=["PUT"])
@check_session_validity
def update_alert(alert_id):
    try:
        alert = _get_owned_alert(alert_id)
        if alert is None:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        if alert.status == "TRIGGERED":
            return (
                jsonify({"status": "error", "message": "A triggered alert cannot be edited"}),
                400,
            )
        data = _json_body()
        clean, error = _validate_payload(data, partial=True, current=alert)
        if error:
            return jsonify({"status": "error", "message": error}), 400
        if not clean:
            return jsonify({"status": "error", "message": "No fields to update"}), 400
        for field, value in clean.items():
            setattr(alert, field, value)
        alert.updated_at = datetime.now()
        db_session.commit()
        api_key = get_api_key_for_tradingview(alert.user_id)
        if api_key:
            alert_engine.unregister_alert(alert.id)
            alert_engine.register_alert(alert.id, alert.user_id, api_key)
        return jsonify({"status": "success", "alert": _serialize_alert(alert)})
    except Exception:
        logger.exception("Failed to update alert")
        db_session.rollback()
        return jsonify({"status": "error", "message": "Failed to update alert"}), 500


@alerts_bp.route("/<alert_id>", methods=["DELETE"])
@check_session_validity
def delete_alert(alert_id):
    try:
        alert = _get_owned_alert(alert_id)
        if alert is None:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        coords = (alert.user_id, alert.symbol, alert.exchange)
        db_session.delete(alert)
        db_session.commit()
        alert_engine.unregister_alert(alert_id, *coords)
        return jsonify({"status": "success"})
    except Exception:
        logger.exception("Failed to delete alert")
        db_session.rollback()
        return jsonify({"status": "error", "message": "Failed to delete alert"}), 500


@alerts_bp.route("/<alert_id>/enable", methods=["POST"])
@check_session_validity
def enable_alert(alert_id):
    try:
        alert = _get_owned_alert(alert_id)
        if alert is None:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        if alert.status == "TRIGGERED":
            return (
                jsonify({"status": "error", "message": "A triggered alert cannot be re-enabled"}),
                400,
            )
        alert.enabled = True
        alert.status = "ACTIVE"
        alert.updated_at = datetime.now()
        db_session.commit()
        api_key = get_api_key_for_tradingview(alert.user_id)
        if api_key:
            alert_engine.register_alert(alert.id, alert.user_id, api_key)
        return jsonify({"status": "success", "alert": _serialize_alert(alert)})
    except Exception:
        logger.exception("Failed to enable alert")
        db_session.rollback()
        return jsonify({"status": "error", "message": "Failed to enable alert"}), 500


@alerts_bp.route("/<alert_id>/disable", methods=["POST"])
@check_session_validity
def disable_alert(alert_id):
    try:
        alert = _get_owned_alert(alert_id)
        if alert is None:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        alert.enabled = False
        alert.updated_at = datetime.now()
        db_session.commit()
        alert_engine.unregister_alert(
            alert_id, alert.user_id, alert.symbol, alert.exchange
        )
        return jsonify({"status": "success", "alert": _serialize_alert(alert)})
    except Exception:
        logger.exception("Failed to disable alert")
        db_session.rollback()
        return jsonify({"status": "error", "message": "Failed to disable alert"}), 500


@alerts_bp.route("/<alert_id>/logs", methods=["GET"])
@check_session_validity
def alert_logs(alert_id):
    try:
        alert = _get_owned_alert(alert_id)
        if alert is None:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        events = (
            db_session.query(AlertEvent)
            .filter(AlertEvent.alert_id == alert_id)
            .order_by(AlertEvent.created_at.desc())
            .limit(50)
            .all()
        )
        event_ids = [e.id for e in events]
        deliveries = (
            db_session.query(AlertDelivery)
            .filter(AlertDelivery.alert_event_id.in_(event_ids or [""]))
            .order_by(AlertDelivery.id.desc())
            .all()
        ) if event_ids else []
        by_event: dict[str, list] = {}
        for d in deliveries:
            by_event.setdefault(d.alert_event_id, []).append(
                {
                    "status": d.status,
                    "attempt": d.attempt,
                    "http_status": d.http_status,
                    "error": d.error,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "completed_at": (
                        d.completed_at.isoformat() if d.completed_at else None
                    ),
                }
            )
        logs = [
            {
                "id": e.id,
                "event_type": e.event_type,
                "signal": e.signal,
                "symbol": e.symbol,
                "price": e.price,
                "bar_time": e.bar_time,
                "idempotency_key": e.idempotency_key,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "deliveries": by_event.get(e.id, []),
            }
            for e in events
        ]
        return jsonify({"status": "success", "logs": logs})
    except Exception:
        logger.exception("Failed to fetch alert logs")
        return jsonify({"status": "error", "message": "Failed to fetch alert logs"}), 500


@alerts_bp.route("/test", methods=["POST"])
@check_session_validity
@limiter.limit("5 per minute")
def test_webhook():
    try:
        data = _json_body()
        url = str(data.get("webhook_url") or "").strip()
        ok, reason = validate_webhook_url(url)
        if not ok:
            return jsonify({"status": "error", "message": reason}), 400
        result = alert_engine.test_webhook(url, _user_id() or "")
        return jsonify(result)
    except Exception:
        logger.exception("Test webhook failed")
        return jsonify({"status": "error", "message": "Test webhook failed"}), 500
