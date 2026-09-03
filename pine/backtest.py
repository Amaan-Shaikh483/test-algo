"""Backtesting on top of the same Pine runtime used everywhere else.

The simulator inside ``PineRuntime`` already produces trades with next-bar
fills; this module only walks the trade list and derives the metrics
(equity curve, drawdown, profit factor, win rate). Keeping metrics out of the
runtime guarantees backtest numbers and chart markers can never diverge.
"""

from dataclasses import dataclass, field

from pine.runtime import Bar, PineRuntime, RuntimeConfig, RuntimeResult


@dataclass
class BacktestMetrics:
    """Summary statistics for one backtest run."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    initial_capital: float = 0.0
    final_equity: float = 0.0
    return_pct: float = 0.0
    equity_curve: list = field(default_factory=list)  # [{time, equity}]
    trade_list: list = field(default_factory=list)


def run_backtest(
    bars: list[Bar],
    script,
    inputs: dict | None = None,
    config: RuntimeConfig | None = None,
) -> tuple[RuntimeResult, BacktestMetrics]:
    """Run the runtime over ``bars`` and compute metrics.

    Returns the raw runtime result (plots, markers, trades) together with the
    metric summary so the UI renders one consistent picture.
    """
    runtime = PineRuntime(script, inputs=inputs, config=config)
    for bar in bars:
        runtime.process_bar(bar, realtime=False)

    result = runtime.result()
    metrics = _metrics_from(result, runtime.config)
    return result, metrics


def _metrics_from(result: RuntimeResult, config: RuntimeConfig) -> BacktestMetrics:
    metrics = BacktestMetrics(initial_capital=config.initial_capital)
    equity = config.initial_capital
    peak = equity
    max_dd = 0.0
    equity_curve: list[dict] = []

    trades = sorted(result.trades, key=lambda t: t.exit_time)
    for trade in trades:
        pnl = trade.pnl
        if config.commission_pct:
            pnl -= (trade.entry_price + trade.exit_price) * trade.qty * config.commission_pct / 100.0
        equity += pnl
        if pnl >= 0:
            metrics.winning_trades += 1
            metrics.gross_profit += pnl
        else:
            metrics.losing_trades += 1
            metrics.gross_loss += abs(pnl)
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        equity_curve.append({"time": trade.exit_time, "equity": round(equity, 2)})

    metrics.total_trades = len(trades)
    metrics.win_rate = (
        metrics.winning_trades / metrics.total_trades * 100.0 if metrics.total_trades else 0.0
    )
    metrics.net_profit = metrics.gross_profit - metrics.gross_loss
    metrics.profit_factor = (
        metrics.gross_profit / metrics.gross_loss if metrics.gross_loss > 0 else float("inf")
    )
    metrics.max_drawdown = max_dd
    metrics.final_equity = equity
    metrics.return_pct = (
        (metrics.final_equity - config.initial_capital) / config.initial_capital * 100.0
        if config.initial_capital
        else 0.0
    )
    metrics.equity_curve = equity_curve
    metrics.trade_list = [
        {
            "entry_id": trade.entry_id,
            "direction": trade.direction,
            "qty": trade.qty,
            "entry_time": trade.entry_time,
            "entry_price": trade.entry_price,
            "exit_time": trade.exit_time,
            "exit_price": trade.exit_price,
            "pnl": round(trade.pnl, 2),
            "exit_reason": trade.exit_reason,
        }
        for trade in trades
    ]
    return metrics
