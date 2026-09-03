"""HTTP API for the Pine strategy engine on the /trading terminal.

Session-authenticated JSON endpoints following the python_strategy blueprint
conventions. The browser is only a client: compile/evaluate run server-side,
and once an instance is started the server-side runner owns execution.

Routes:
    POST /pine/compile                     - compile + validate, return errors/inputs
    POST /pine/evaluate                    - historical run -> plots/markers/signals
    POST /pine/backtest                    - backtest with metrics
    GET  /pine/scripts                     - list saved scripts
    POST /pine/scripts                     - save a script
    GET  /pine/scripts/<id>                - load a script
    PUT  /pine/scripts/<id>                - update a script
    DELETE /pine/scripts/<id>              - delete a script
    GET  /pine/strategies                  - list strategy instances
    POST /pine/strategies                  - create an instance
    GET  /pine/strategies/<id>             - instance detail + live status
    DELETE /pine/strategies/<id>           - delete (stops first)
    POST /pine/strategies/<id>/start       - start server-side execution
    POST /pine/strategies/<id>/pause       - pause
    POST /pine/strategies/<id>/resume      - resume
    POST /pine/strategies/<id>/stop        - stop
    POST /pine/strategies/<id>/live        - enable LIVE mode (explicit confirm)
    POST /pine/strategies/<id>/paper       - switch back to PAPER mode
    GET  /pine/strategies/<id>/signals     - signal history
    GET  /pine/strategies/<id>/alerts      - alert history
    GET  /pine/strategies/<id>/orders      - executed signals (order view)
"""

import json

from flask import Blueprint, jsonify, request, session

from database import pine_db
from database.auth_db import get_api_key_for_tradingview
from limiter import limiter
from services.pine_strategy_service import (
    backtest_script,
    evaluate_script,
    manager,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

pine_bp = Blueprint("pine_bp", __name__, url_prefix="/pine")

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


def _api_key(user_id: str) -> str | None:
    return get_api_key_for_tradingview(user_id)


def _json_body() -> dict:
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# Compile / evaluate / backtest
# ---------------------------------------------------------------------------


@pine_bp.route("/compile", methods=["POST"])
@check_session_validity
@limiter.limit("60 per minute")
def compile_pine():
    """Compile a script and return structured diagnostics + input defs."""
    body = _json_body()
    code = body.get("code", "")
    if not code or not isinstance(code, str):
        return jsonify({"status": "error", "error": {
            "type": "compile_error", "line": 0, "column": 0,
            "message": "No Pine code provided"}}), 400

    from pine.compiler import compile_script

    result = compile_script(code)
    return jsonify(result.to_dict())


@pine_bp.route("/evaluate", methods=["POST"])
@check_session_validity
@limiter.limit("30 per minute")
def evaluate_pine():
    """Historical evaluation for Add-to-Chart: plots, markers, signals."""
    user_id = _user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    body = _json_body()

    code = body.get("code", "")
    symbol = body.get("symbol", "")
    exchange = body.get("exchange", "")
    timeframe = body.get("timeframe", "")
    inputs = body.get("inputs") or {}

    if not code or not symbol or not exchange or not timeframe:
        return jsonify({"status": "error", "message": "code, symbol, exchange and timeframe are required"}), 400
    if timeframe not in VALID_TIMEFRAMES:
        return jsonify({"status": "error", "message": f"Unsupported timeframe {timeframe}"}), 400

    api_key = _api_key(user_id)
    if not api_key:
        return jsonify({"status": "error", "message": "No API key configured for this account"}), 400

    ok, payload = evaluate_script(code, symbol, exchange, timeframe, api_key, inputs)
    if not ok:
        return jsonify({"status": "error", **payload}), 400
    return jsonify({"status": "success", **payload})


@pine_bp.route("/backtest", methods=["POST"])
@check_session_validity
@limiter.limit("20 per minute")
def backtest_pine():
    """Backtest using the same runtime as live execution."""
    user_id = _user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    body = _json_body()

    code = body.get("code", "")
    symbol = body.get("symbol", "")
    exchange = body.get("exchange", "")
    timeframe = body.get("timeframe", "")
    inputs = body.get("inputs") or {}
    config = body.get("config") or {}

    if not code or not symbol or not exchange or not timeframe:
        return jsonify({"status": "error", "message": "code, symbol, exchange and timeframe are required"}), 400
    if timeframe not in VALID_TIMEFRAMES:
        return jsonify({"status": "error", "message": f"Unsupported timeframe {timeframe}"}), 400

    api_key = _api_key(user_id)
    if not api_key:
        return jsonify({"status": "error", "message": "No API key configured for this account"}), 400

    ok, payload = backtest_script(code, symbol, exchange, timeframe, api_key, inputs, config)
    if not ok:
        return jsonify({"status": "error", **payload}), 400

    # Persist the run for later inspection (best effort).
    try:
        pine_db.record_backtest(
            script_id=int(body.get("script_id") or 0),
            user_id=user_id,
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            params=json.dumps({"inputs": inputs, "config": config}),
            metrics=json.dumps(payload.get("metrics", {})),
        )
    except Exception:
        logger.exception("Failed to persist Pine backtest run")

    return jsonify({"status": "success", **payload})


# ---------------------------------------------------------------------------
# Saved scripts
# ---------------------------------------------------------------------------


@pine_bp.route("/scripts", methods=["GET"])
@check_session_validity
def list_scripts():
    user_id = _user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    scripts = pine_db.get_user_scripts(user_id)
    return jsonify({
        "status": "success",
        "scripts": [
            {"id": s.id, "name": s.name, "kind": s.kind, "updated_at": s.updated_at.isoformat() if s.updated_at else None}
            for s in scripts
        ],
    })


@pine_bp.route("/scripts", methods=["POST"])
@check_session_validity
def save_script():
    user_id = _user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    body = _json_body()
    name = (body.get("name") or "").strip()
    code = body.get("code", "")
    if not name or not code:
        return jsonify({"status": "error", "message": "name and code are required"}), 400

    from pine.compiler import compile_script

    compiled = compile_script(code)
    script = pine_db.create_script(user_id, name, code, compiled.kind if compiled.ok else "strategy")
    if script is None:
        return jsonify({"status": "error", "message": "Could not save the script"}), 500
    return jsonify({"status": "success", "id": script.id})


@pine_bp.route("/scripts/<int:script_id>", methods=["GET"])
@check_session_validity
def load_script(script_id: int):
    user_id = _user_id()
    script = pine_db.get_script(script_id)
    if script is None or script.user_id != user_id:
        return jsonify({"status": "error", "message": "Script not found"}), 404
    return jsonify({
        "status": "success",
        "script": {"id": script.id, "name": script.name, "code": script.code, "kind": script.kind},
    })


@pine_bp.route("/scripts/<int:script_id>", methods=["PUT"])
@check_session_validity
def update_script(script_id: int):
    user_id = _user_id()
    script = pine_db.get_script(script_id)
    if script is None or script.user_id != user_id:
        return jsonify({"status": "error", "message": "Script not found"}), 404
    body = _json_body()
    code = body.get("code")
    name = body.get("name")
    if code is None and name is None:
        return jsonify({"status": "error", "message": "Nothing to update"}), 400
    if not pine_db.update_script(script_id, code=code, name=name):
        return jsonify({"status": "error", "message": "Could not update the script"}), 500
    return jsonify({"status": "success"})


@pine_bp.route("/scripts/<int:script_id>", methods=["DELETE"])
@check_session_validity
def delete_script(script_id: int):
    user_id = _user_id()
    script = pine_db.get_script(script_id)
    if script is None or script.user_id != user_id:
        return jsonify({"status": "error", "message": "Script not found"}), 404
    pine_db.delete_script(script_id)
    return jsonify({"status": "success"})


# ---------------------------------------------------------------------------
# Strategy instances
# ---------------------------------------------------------------------------


def _instance_payload(instance) -> dict:
    runner = manager.get_runner(instance.id)
    return {
        "id": instance.id,
        "script_id": instance.script_id,
        "name": instance.name,
        "symbol": instance.symbol,
        "exchange": instance.exchange,
        "timeframe": instance.timeframe,
        "status": "RUNNING" if runner and not runner.paused and instance.status != "PAUSED" else instance.status,
        "execution_mode": instance.execution_mode,
        "quantity": instance.quantity,
        "product": instance.product,
        "inputs": json.loads(instance.inputs or "{}"),
        "last_bar_time": instance.last_bar_time,
        "last_signal_time": instance.last_signal_time.isoformat() if instance.last_signal_time else None,
        "last_error": instance.last_error,
        "created_at": instance.created_at.isoformat() if instance.created_at else None,
        "live_confirmed": bool(instance.live_confirmed),
    }


@pine_bp.route("/strategies", methods=["GET"])
@check_session_validity
def list_strategies():
    user_id = _user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    instances = pine_db.get_user_instances(user_id)
    return jsonify({
        "status": "success",
        "strategies": [_instance_payload(i) for i in instances],
    })


@pine_bp.route("/strategies", methods=["POST"])
@check_session_validity
def create_strategy():
    user_id = _user_id()
    if not user_id:
        return jsonify({"status": "error", "message": "Not authenticated"}), 401
    body = _json_body()

    script_id = body.get("script_id")
    name = (body.get("name") or "").strip()
    symbol = (body.get("symbol") or "").strip().upper()
    exchange = (body.get("exchange") or "").strip().upper()
    timeframe = body.get("timeframe", "")
    quantity = int(body.get("quantity") or 1)
    product = body.get("product", "MIS")
    inputs = body.get("inputs") or {}

    if not script_id or not name or not symbol or not exchange or not timeframe:
        return jsonify({"status": "error", "message": "script_id, name, symbol, exchange, timeframe are required"}), 400
    if exchange not in VALID_EXCHANGES:
        return jsonify({"status": "error", "message": f"Unsupported exchange {exchange}"}), 400
    if timeframe not in VALID_TIMEFRAMES:
        return jsonify({"status": "error", "message": f"Unsupported timeframe {timeframe}"}), 400
    if quantity < 1:
        return jsonify({"status": "error", "message": "quantity must be >= 1"}), 400
    if product not in ("MIS", "CNC", "NRML"):
        return jsonify({"status": "error", "message": "product must be MIS, CNC or NRML"}), 400

    script = pine_db.get_script(script_id)
    if script is None or script.user_id != user_id:
        return jsonify({"status": "error", "message": "Script not found"}), 404

    instance = pine_db.create_instance(
        script_id=script_id,
        user_id=user_id,
        name=name,
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
        status="STOPPED",
        execution_mode="PAPER",  # NEVER default to live
        broker="",
        quantity=quantity,
        product=product,
        inputs=json.dumps(inputs),
        live_confirmed=False,
    )
    if instance is None:
        return jsonify({"status": "error", "message": "Could not create the strategy"}), 500
    return jsonify({"status": "success", "strategy": _instance_payload(instance)})


def _get_own_instance(instance_id: str, user_id: str):
    instance = pine_db.get_instance(instance_id)
    if instance is None or instance.user_id != user_id:
        return None
    return instance


@pine_bp.route("/strategies/<instance_id>", methods=["GET"])
@check_session_validity
def get_strategy(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    return jsonify({"status": "success", "strategy": _instance_payload(instance)})


@pine_bp.route("/strategies/<instance_id>", methods=["DELETE"])
@check_session_validity
def delete_strategy(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    manager.stop(instance)
    pine_db.delete_instance(instance_id)
    return jsonify({"status": "success"})


@pine_bp.route("/strategies/<instance_id>/start", methods=["POST"])
@check_session_validity
@limiter.limit("30 per minute")
def start_strategy(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404

    api_key = _api_key(user_id)
    if not api_key:
        return jsonify({"status": "error", "message": "No API key configured for this account"}), 400

    script = pine_db.get_script(instance.script_id)
    if script is None:
        return jsonify({"status": "error", "message": "Script not found"}), 404

    ok, message = manager.start(instance, script.code, api_key)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "success", "message": message})


@pine_bp.route("/strategies/<instance_id>/pause", methods=["POST"])
@check_session_validity
def pause_strategy(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    ok, message = manager.pause(instance)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "success", "message": message})


@pine_bp.route("/strategies/<instance_id>/resume", methods=["POST"])
@check_session_validity
def resume_strategy(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    ok, message = manager.resume(instance)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "success", "message": message})


@pine_bp.route("/strategies/<instance_id>/stop", methods=["POST"])
@check_session_validity
def stop_strategy(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    ok, message = manager.stop(instance)
    if not ok:
        return jsonify({"status": "error", "message": message}), 400
    return jsonify({"status": "success", "message": message})


@pine_bp.route("/strategies/<instance_id>/live", methods=["POST"])
@check_session_validity
def enable_live(instance_id: str):
    """Enable LIVE mode. Requires an explicit confirmation in the payload.

    The strategy can never silently switch from PAPER to LIVE: the client must
    send {"confirm": true} and the instance must already exist; switching back
    is always allowed.
    """
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    body = _json_body()
    if body.get("confirm") is not True:
        return jsonify({
            "status": "error",
            "message": "Live trading requires explicit confirmation: send {\"confirm\": true}",
        }), 400

    # A strategy that is running must be restarted to pick up the new mode.
    was_running = manager.get_runner(instance_id) is not None
    if was_running:
        manager.stop(instance)

    pine_db.update_instance(instance_id, execution_mode="LIVE", live_confirmed=True)
    instance = pine_db.get_instance(instance_id)

    if was_running:
        api_key = _api_key(user_id)
        script = pine_db.get_script(instance.script_id)
        if api_key and script is not None:
            manager.start(instance, script.code, api_key)

    logger.warning(
        "Pine strategy '%s' (%s) switched to LIVE trading by user %s",
        instance.name, instance_id, user_id,
    )
    return jsonify({"status": "success", "strategy": _instance_payload(instance)})


@pine_bp.route("/strategies/<instance_id>/paper", methods=["POST"])
@check_session_validity
def enable_paper(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404

    was_running = manager.get_runner(instance_id) is not None
    if was_running:
        manager.stop(instance)
    pine_db.update_instance(instance_id, execution_mode="PAPER", live_confirmed=False)
    instance = pine_db.get_instance(instance_id)
    if was_running:
        api_key = _api_key(user_id)
        script = pine_db.get_script(instance.script_id)
        if api_key and script is not None:
            manager.start(instance, script.code, api_key)
    return jsonify({"status": "success", "strategy": _instance_payload(instance)})


@pine_bp.route("/strategies/<instance_id>/signals", methods=["GET"])
@check_session_validity
def strategy_signals(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    signals = pine_db.get_instance_signals(instance_id)
    return jsonify({
        "status": "success",
        "signals": [
            {
                "signal_id": s.signal_id,
                "signal": s.signal,
                "kind": s.kind,
                "order_ref": s.order_ref,
                "symbol": s.symbol,
                "exchange": s.exchange,
                "timeframe": s.timeframe,
                "price": s.price,
                "quantity": s.quantity,
                "bar_time": s.bar_time,
                "source": s.source,
                "executed": bool(s.executed),
                "order_id": s.order_id,
                "order_status": s.order_status,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in reversed(signals)
        ],
    })


@pine_bp.route("/strategies/<instance_id>/alerts", methods=["GET"])
@check_session_validity
def strategy_alerts(instance_id: str):
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    alerts = pine_db.get_instance_alerts(instance_id)
    return jsonify({
        "status": "success",
        "alerts": [
            {
                "kind": a.kind,
                "title": a.title,
                "message": a.message,
                "bar_time": a.bar_time,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in reversed(alerts)
        ],
    })


@pine_bp.route("/strategies/<instance_id>/orders", methods=["GET"])
@check_session_validity
def strategy_orders(instance_id: str):
    """Executed signals: the order/execution view of this strategy."""
    user_id = _user_id()
    instance = _get_own_instance(instance_id, user_id)
    if instance is None:
        return jsonify({"status": "error", "message": "Strategy not found"}), 404
    signals = [s for s in pine_db.get_instance_signals(instance_id) if s.executed]
    return jsonify({
        "status": "success",
        "orders": [
            {
                "signal_id": s.signal_id,
                "signal": s.signal,
                "order_id": s.order_id,
                "order_status": s.order_status,
                "price": s.price,
                "quantity": s.quantity,
                "bar_time": s.bar_time,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in reversed(signals)
        ],
    })
