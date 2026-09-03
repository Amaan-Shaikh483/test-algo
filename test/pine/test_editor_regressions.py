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
"""

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
