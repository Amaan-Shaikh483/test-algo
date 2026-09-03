"""Strategy function tests: entry/close/exit semantics, alerts, backtest."""

from pine_test_utils import make_bars, trend_bars

from pine.backtest import run_backtest
from pine.compiler import compile_script
from pine.runtime import NA, Bar, PineRuntime, RuntimeConfig


def run_script(source: str, bars: list[Bar], inputs: dict | None = None, config=None):
    result = compile_script(source)
    assert result.ok, result.error
    runtime = PineRuntime(result.script, inputs=inputs, config=config)
    for bar in bars:
        runtime.process_bar(bar)
    return runtime


class TestStrategyEntry:
    def test_entry_generates_signal_intent(self):
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 5\n'
            '    strategy.entry("BUY", strategy.long)\n',
            trend_bars((1,), 20),
        )
        signals = runtime.signals
        assert len(signals) == 1
        assert signals[0].signal == "BUY"
        assert signals[0].kind == "entry"
        assert signals[0].bar_index == 5

    def test_entry_fills_next_bar_open(self):
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 5\n'
            '    strategy.entry("BUY", strategy.long)\n',
            trend_bars((1,), 20),
        )
        entry = list(runtime.sim.trades)
        # Position opens at bar 6's open
        assert runtime.sim.position == 1.0
        assert entry == []  # not closed yet, so no completed trade
        bar6 = runtime.bars[6]
        assert runtime.sim.avg_price == bar6.open

    def test_opposite_entry_reverses(self):
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 2\n'
            '    strategy.entry("L", strategy.long)\n'
            'if bar_index == 10\n'
            '    strategy.entry("S", strategy.short)\n',
            trend_bars((1,), 20),
        )
        trades = runtime.sim.trades
        assert len(trades) == 1  # the long was closed by the reversal
        assert trades[0].exit_reason == "opposite"
        assert runtime.sim.position == -1.0

    def test_short_disabled(self):
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 2\n'
            '    strategy.entry("S", strategy.short)\n',
            trend_bars((1,), 10),
            config=RuntimeConfig(short_enabled=False),
        )
        assert runtime.sim.position == 0.0
        assert runtime.signals == []


class TestStrategyClose:
    def test_close_closes_long(self):
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 2\n'
            '    strategy.entry("BUY", strategy.long)\n'
            'if bar_index == 10\n'
            '    strategy.close("BUY")\n',
            trend_bars((1,), 20),
        )
        trades = runtime.sim.trades
        assert len(trades) == 1
        assert trades[0].exit_reason == "close"
        assert runtime.sim.position == 0.0
        close_signal = [s for s in runtime.signals if s.kind == "close"]
        assert len(close_signal) == 1
        assert close_signal[0].signal == "SELL"

    def test_close_all(self):
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 2\n'
            '    strategy.entry("BUY", strategy.long)\n'
            'if bar_index == 10\n'
            '    strategy.close_all()\n',
            trend_bars((1,), 20),
        )
        assert runtime.sim.position == 0.0
        assert len(runtime.sim.trades) == 1


class TestStrategyExit:
    def test_tight_stop_triggers_intrabar(self):
        # Rising bars: enter long at bar 2 (fills bar 3 open); place the stop
        # above bar 4's low so bar 4 trades through it.
        bars = trend_bars((1,), 30)
        stop = bars[4].low + 0.05
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 2\n'
            '    strategy.entry("BUY", strategy.long)\n'
            f'if bar_index == 3\n'
            f'    strategy.exit("XL", from_entry="BUY", stop={stop:.4f})\n',
            bars,
        )
        trades = runtime.sim.trades
        assert len(trades) == 1
        assert trades[0].exit_reason == "stop"
        assert abs(trades[0].exit_price - stop) < 1e-6
        assert runtime.sim.position == 0.0

    def test_take_profit_limit(self):
        # Enter long at bar 2 (fills bar 3 open); take profit just under
        # bar 4's high so bar 4 reaches it.
        bars = trend_bars((1,), 30)
        limit = bars[4].high - 0.05
        runtime = run_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 2\n'
            '    strategy.entry("BUY", strategy.long)\n'
            f'if bar_index == 3\n'
            f'    strategy.exit("XL", from_entry="BUY", limit={limit:.4f})\n',
            bars,
        )
        trades = runtime.sim.trades
        assert len(trades) == 1
        assert trades[0].exit_reason == "limit"


class TestAlerts:
    def test_alertcondition_fires_on_true(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'alertcondition(close > open, "Up", "Close above open")\n',
            trend_bars((1, -1), 10),
        )
        titles = [a.title for a in runtime.alerts]
        assert "Up" in titles

    def test_alert_fires_once_per_bar(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'if close > open\n'
            '    alert("up", alert.freq_once_per_bar)\n',
            trend_bars((1,), 10),
        )
        assert len(runtime.alerts) == 10  # once per bar

    def test_alert_messages(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'alertcondition(close > open, "Up", "Close above open")\n',
            trend_bars((1,), 5),
        )
        assert all(a.message == "Close above open" for a in runtime.alerts)


class TestRealtimeMode:
    def test_realtime_fills_at_close(self):
        bars = trend_bars((1,), 30)
        result = compile_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 5\n'
            '    strategy.entry("BUY", strategy.long)\n'
        )
        assert result.ok
        runtime = PineRuntime(result.script)
        for bar in bars[:6]:
            runtime.process_bar(bar, realtime=True)
        # The order emitted on bar 5 fills immediately at bar 5's close
        assert runtime.sim.position == 1.0
        assert runtime.sim.avg_price == bars[5].close

    def test_realtime_signal_emitted_once(self):
        bars = trend_bars((1,), 30)
        result = compile_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 5\n'
            '    strategy.entry("BUY", strategy.long)\n'
        )
        runtime = PineRuntime(result.script)
        emitted_total = []
        for bar in bars[:8]:
            emitted_total.extend(runtime.process_bar(bar, realtime=True))
        assert len(emitted_total) == 1
        assert emitted_total[0].signal == "BUY"

    def test_historical_pending_discarded(self):
        bars = trend_bars((1,), 30)
        result = compile_script(
            '//@version=5\nstrategy("S")\n'
            'if bar_index == 25\n'
            '    strategy.entry("BUY", strategy.long)\n'
        )
        runtime = PineRuntime(result.script)
        for bar in bars:
            runtime.process_bar(bar)
        runtime.discard_pending()
        assert runtime.sim.pending == []


class TestBacktest:
    def test_metrics_computed(self):
        source = (
            '//@version=5\nstrategy("S")\n'
            "fast = ta.ema(close, 3)\nslow = ta.ema(close, 8)\n"
            "if ta.crossover(fast, slow)\n    strategy.entry(\"L\", strategy.long)\n"
            "if ta.crossunder(fast, slow)\n    strategy.entry(\"S\", strategy.short)\n"
        )
        result = compile_script(source)
        assert result.ok
        bars = trend_bars((1, -1, 1, -1), 40)
        runtime_result, metrics = run_backtest(bars, result.script)
        assert metrics.total_trades > 0
        assert metrics.winning_trades + metrics.losing_trades == metrics.total_trades
        assert abs(metrics.net_profit - (metrics.gross_profit - metrics.gross_loss)) < 1e-9
        assert metrics.initial_capital == 100000.0
        assert len(metrics.equity_curve) == metrics.total_trades
        assert len(metrics.trade_list) == metrics.total_trades

    def test_commission_reduces_pnl(self):
        source = (
            '//@version=5\nstrategy("S")\n'
            "if bar_index == 2\n    strategy.entry(\"L\", strategy.long)\n"
            "if bar_index == 10\n    strategy.close(\"L\")\n"
        )
        result = compile_script(source)
        bars = trend_bars((1,), 30)
        _, no_commission = run_backtest(bars, result.script)
        _, with_commission = run_backtest(
            bars, result.script, config=RuntimeConfig(commission_pct=0.1)
        )
        assert with_commission.net_profit < no_commission.net_profit

    def test_initial_capital_respected(self):
        source = '//@version=5\nstrategy("S")\nif bar_index == 2\n    strategy.entry("L", strategy.long)\n'
        result = compile_script(source)
        bars = trend_bars((1,), 10)
        _, metrics = run_backtest(bars, result.script, config=RuntimeConfig(initial_capital=50000))
        assert metrics.initial_capital == 50000
