"""Regression tests for the default editor script and order-function guards.

These cover three bugs found in live use of the /trading Pine editor:

1. ``input`` sat in UNSUPPORTED_KEYWORDS, so ``input.int(9, "Fast EMA")``
   failed with "Unsupported Pine feature: 'input' keyword" — the lexer now
   treats ``input`` followed by ``(`` or ``.`` as the function family and
   still rejects the ``input int x`` type qualifier.
2. ``color.new`` / ``color.rgb`` were implemented in the runtime but left in
   UNSUPPORTED_FEATURES, so the validator rejected them.
3. ``strategy.entry`` / ``strategy.close`` ignored the ``when=`` kwarg, which
   submitted an order every bar and flooded the signal log (and the chart
   markers) with an entry/close pair per bar.
4. evaluate_script()/backtest_script() crashed with a NameError from the
   ``_Instance`` class body, which the app's 500 handler answered with a
   redirect the browser followed to an HTML page - the editor could only
   show a bare "Evaluation failed"/"Backtest failed".
"""

from datetime import UTC, datetime, timezone

import pytest
from pine_test_utils import trend_bars

from pine.compiler import compile_script
from pine.runtime import Bar, PineRuntime

DEFAULT_EDITOR_SCRIPT = '''//@version=5
indicator("EMA Cross", overlay=true)

fast = input.int(9, "Fast EMA")
slow = input.int(21, "Slow EMA")

emaFast = ta.ema(close, fast)
emaSlow = ta.ema(close, slow)

bull = ta.crossover(emaFast, emaSlow)
bear = ta.crossunder(emaFast, emaSlow)

plot(emaFast, "Fast", color=color.new("#2962ff", 0))
plot(emaSlow, "Slow", color=color.new("#089981", 0))

plotshape(bull, style=shape.triangleup, location=location.belowbar, color=color.new("#089981", 0))
plotshape(bear, style=shape.triangledown, location=location.abovebar, color=color.new("#f23645", 0))
'''


def run(source: str, bars: list[Bar] | None = None) -> PineRuntime:
    result = compile_script(source)
    assert result.ok, f"{result.error.line}:{result.error.column} {result.error.message}"
    runtime = PineRuntime(result.script, inputs={})
    for bar in bars if bars is not None else trend_bars((1,), 30):
        runtime.process_bar(bar)
    return runtime


class TestInputKeyword:
    def test_default_editor_script_compiles(self):
        result = compile_script(DEFAULT_EDITOR_SCRIPT)
        assert result.ok, f"{result.error.line}:{result.error.column} {result.error.message}"
        assert result.title == "EMA Cross"
        assert result.overlay is True
        assert [i["name"] for i in result.inputs] == ["fast", "slow"]

    def test_input_namespaced_calls_compile(self):
        for call in ["input.int(9)", "input.float(1.5)", "input.bool(true)", 'input.string("x")']:
            source = f'//@version=5\nindicator("I")\nx = {call}\nplot(close)'
            assert compile_script(source).ok, call

    def test_plain_input_call_compiles(self):
        source = '//@version=5\nindicator("I")\nx = input(14, "Length")\nplot(close)'
        assert compile_script(source).ok

    def test_input_type_qualifier_still_rejected(self):
        result = compile_script('//@version=5\nindicator("I")\nx = input int 5\nplot(x)')
        assert not result.ok
        assert result.error.kind == "unsupported_feature"
        assert "'input' keyword" in result.error.message


class TestColorFunctions:
    def test_color_new_compiles_and_evaluates(self):
        runtime = run(
            '//@version=5\nindicator("C")\nplot(close, "P", color=color.new("#2962ff", 0))'
        )
        out = runtime.result()
        assert out.plots[0].color == "#2962ff"

    def test_color_named_base_resolves(self):
        runtime = run(
            '//@version=5\nindicator("C")\nplot(close, "P", color=color.new(color.red, 0))'
        )
        out = runtime.result()
        assert out.plots[0].color == "#ef5350"

    def test_color_rgb(self):
        runtime = run(
            '//@version=5\nindicator("C")\nplot(close, "P", color=color.rgb(41, 98, 255))'
        )
        out = runtime.result()
        assert out.plots[0].color == "#2962ff"

    def test_plot_title_from_positional_arg(self):
        runtime = run('//@version=5\nindicator("C")\nplot(close, "My Line")')
        out = runtime.result()
        assert out.plots[0].title == "My Line"


class TestWhenGuard:
    SCRIPT = '''//@version=5
strategy("EMA Cross", overlay=true)
bull = ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
bear = ta.crossunder(ta.ema(close, 9), ta.ema(close, 21))
strategy.entry("L", "long", when=bull)
strategy.close("L", when=bear)
'''

    def test_no_signal_flood(self):
        runtime = run(self.SCRIPT, trend_bars((1,), 60))
        out = runtime.result()
        # One entry per crossover and one close per crossunder — never a
        # pair on every bar, which is what the missing guard produced.
        assert len(out.signals) <= 6, [(s.signal, s.kind) for s in out.signals]

    def test_full_cycle_decline_rally_decline(self):
        bars = trend_bars((-1,) * 20 + (2,) * 40 + (-2.5,) * 20)
        runtime = run(self.SCRIPT, bars)
        out = runtime.result()
        assert [(s.signal, s.kind) for s in out.signals] == [("BUY", "entry"), ("SELL", "close")]
        assert len(out.trades) == 1
        assert out.trades[0].direction == "long"
        assert out.trades[0].pnl > 0

    def test_when_false_never_orders(self):
        runtime = run(
            '//@version=5\nstrategy("N")\nstrategy.entry("L", "long", when=false)',
            trend_bars((1,), 10),
        )
        assert runtime.result().signals == []

    def test_if_block_style_still_orders(self):
        source = '''//@version=5
strategy("If style")
bull = ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
if bull
    strategy.entry("L", "long")
'''
        runtime = run(source, trend_bars((-1, 2), 30))
        out = runtime.result()
        assert [(s.signal, s.kind) for s in out.signals] == [("BUY", "entry")]


class TestSimGuards:
    def test_close_when_flat_is_noop(self):
        runtime = run(
            '//@version=5\nstrategy("C")\nstrategy.close("L", when=true)',
            trend_bars((1,), 10),
        )
        assert runtime.result().signals == []

    def test_same_direction_reentry_ignored(self):
        # pyramiding=0: a second long while already long must not add size.
        source = '''//@version=5
strategy("P")
strategy.entry("A", "long")
strategy.entry("B", "long")
'''
        runtime = run(source, trend_bars((1,), 6))
        assert runtime.sim.position == 1.0
        assert runtime.sim.trades == [] or runtime.sim.trades[0].qty == 1.0

    def test_close_targets_only_named_entry(self):
        source = '''//@version=5
strategy("T")
strategy.entry("A", "long")
strategy.close("B")
'''
        runtime = run(source, trend_bars((1,), 6))
        # "B" was never opened, so the close is ignored and "A" stays open.
        assert runtime.sim.position == 1.0


class TestEvaluateAndBacktestScript:
    """evaluate_script()/backtest_script() end to end.

    These two functions crashed with a NameError until the _Instance class
    body was replaced (class-scope shadowing), which the app's 500 handler
    turned into a redirect - the browser showed a bare "Evaluation failed".
    """

    SCRIPT = '''//@version=5
strategy("EMA Cross", overlay=true)
bull = ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
bear = ta.crossunder(ta.ema(close, 9), ta.ema(close, 21))
plot(ta.ema(close, 9), "Fast")
plot(ta.ema(close, 21), "Slow")
strategy.entry("L", "long", when=bull)
strategy.close("L", when=bear)
'''

    @pytest.fixture()
    def history(self, monkeypatch):
        bars = trend_bars((-1, 2, -2), 25)

        def fake_load(instance, api_key):
            return bars

        import services.pine_strategy_service as pss

        monkeypatch.setattr(pss.manager, "_load_history", fake_load)
        return bars

    def test_evaluate_script_success(self, history):
        from services.pine_strategy_service import evaluate_script

        ok, payload = evaluate_script(self.SCRIPT, "RELIANCE", "NSE", "5m", "key")
        assert ok, payload
        assert payload["meta"]["title"] == "EMA Cross"
        assert payload["meta"]["overlay"] is True
        assert len(payload["plots"]) == 2
        assert payload["plots"][0]["title"] == "Fast"
        assert all("time" in point and "value" in point for point in payload["plots"][0]["data"])
        assert payload["signals"], "crossover entries expected"
        assert payload["trades"], "completed trades expected"
        assert payload["bars_processed"] == len(history)

    def test_evaluate_script_no_history(self, monkeypatch):
        import services.pine_strategy_service as pss
        from services.pine_strategy_service import evaluate_script

        monkeypatch.setattr(
            pss.manager, "_load_history", lambda instance, api_key: None
        )
        ok, payload = evaluate_script(self.SCRIPT, "RELIANCE", "NSE", "5m", "key")
        assert not ok
        assert "No historical data" in payload["error"]["message"]

    def test_backtest_script_success(self, history):
        from services.pine_strategy_service import backtest_script

        ok, payload = backtest_script(
            self.SCRIPT, "RELIANCE", "NSE", "5m", "key",
            config={"initial_capital": 100000, "commission_pct": 0.0},
        )
        assert ok, payload
        metrics = payload["metrics"]
        assert metrics["initial_capital"] == 100000
        assert metrics["total_trades"] >= 1
        assert metrics["equity_curve"]
        assert metrics["trade_list"]
        # The chart payload renders identically for the backtest snapshot.
        assert payload["meta"]["kind"] == "strategy"
        assert len(payload["plots"]) == 2
        assert payload["signals"]

    def test_backtest_script_indicator_without_orders(self, history):
        from services.pine_strategy_service import backtest_script

        source = '//@version=5\nindicator("I")\nplot(close, "Close")'
        ok, payload = backtest_script(source, "RELIANCE", "NSE", "5m", "key")
        assert ok, payload
        assert payload["metrics"]["total_trades"] == 0
        assert payload["metrics"]["final_equity"] == 100000.0

    def test_evaluate_script_compile_error(self, history):
        from services.pine_strategy_service import evaluate_script

        ok, payload = evaluate_script(
            '//@version=5\nindicator("X")\nplot(request.security("A", "1D", close))',
            "RELIANCE", "NSE", "5m", "key",
        )
        assert not ok
        assert "request.security" in payload["error"]["message"]


class TestEvaluateBacktestEndpoints:
    """The /pine/evaluate and /pine/backtest HTTP surfaces.

    Guards the contract the React editor depends on: success JSON carries
    meta/plots, failures carry {status: error, ...} with a readable message -
    never a redirect, which the browser would follow into an HTML page.
    """

    SCRIPT = '//@version=5\nindicator("I")\nplot(close, "Close")'

    @pytest.fixture()
    def client(self, monkeypatch):
        from flask import Flask

        import blueprints.pine as pine_blueprint
        import utils.session as session_utils

        application = Flask(__name__)
        application.config["TESTING"] = True
        application.secret_key = "pine-eval-test"
        application.register_blueprint(pine_blueprint.pine_bp)
        monkeypatch.setattr(
            session_utils, "is_session_valid", lambda: True
        )
        with application.test_client() as test_client:
            with test_client.session_transaction() as s:
                s["logged_in"] = True
                s["user"] = "tester"
                s["login_time"] = datetime.now(UTC).isoformat()
            yield test_client

    def _api_key(self, monkeypatch):
        import blueprints.pine as pine_blueprint

        monkeypatch.setattr(
            pine_blueprint, "get_api_key_for_tradingview", lambda user_id: "testkey"
        )

    def test_evaluate_success(self, client, monkeypatch):
        import services.pine_strategy_service as pss

        self._api_key(monkeypatch)
        monkeypatch.setattr(
            pss.manager, "_load_history", lambda instance, api_key: trend_bars((1,), 30)
        )
        res = client.post("/pine/evaluate", json={
            "code": self.SCRIPT, "symbol": "RELIANCE", "exchange": "NSE", "timeframe": "5m",
        })
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert body["meta"]["title"] == "I"
        assert body["plots"]

    def test_backtest_success(self, client, monkeypatch):
        import services.pine_strategy_service as pss

        self._api_key(monkeypatch)
        monkeypatch.setattr(
            pss.manager, "_load_history", lambda instance, api_key: trend_bars((1,), 30)
        )
        res = client.post("/pine/backtest", json={
            "code": self.SCRIPT, "symbol": "RELIANCE", "exchange": "NSE", "timeframe": "5m",
        })
        assert res.status_code == 200
        body = res.get_json()
        assert body["status"] == "success"
        assert "metrics" in body

    def test_evaluate_no_history_is_json_not_redirect(self, client, monkeypatch):
        import services.pine_strategy_service as pss

        self._api_key(monkeypatch)
        monkeypatch.setattr(pss.manager, "_load_history", lambda instance, api_key: None)
        res = client.post("/pine/evaluate", json={
            "code": self.SCRIPT, "symbol": "RELIANCE", "exchange": "NSE", "timeframe": "5m",
        })
        assert res.status_code == 400
        body = res.get_json()
        assert body["status"] == "error"
        assert "No historical data" in body["error"]["message"]

    def test_internal_error_returns_json_not_redirect(self, client, monkeypatch):
        import services.pine_strategy_service as pss

        self._api_key(monkeypatch)

        import blueprints.pine as pine_blueprint

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(pine_blueprint, "evaluate_script", boom)
        res = client.post("/pine/evaluate", json={
            "code": self.SCRIPT, "symbol": "RELIANCE", "exchange": "NSE", "timeframe": "5m",
        })
        assert res.status_code == 500
        body = res.get_json()
        assert body["status"] == "error"
        assert body["error"]["type"] == "runtime_error"

    def test_missing_fields(self, client):
        res = client.post("/pine/evaluate", json={"code": self.SCRIPT})
        assert res.status_code == 400
        assert res.get_json()["status"] == "error"
