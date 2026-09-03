"""Bar-by-bar interpreter for the Pine subset.

Execution model (identical for chart evaluation, backtesting and realtime):

* Bars are processed strictly sequentially. A bar's OHLCV is exposed to the
  script only while that bar is being processed - there is no look-ahead by
  construction; history access (``close[1]``) can only read already-processed
  bars.
* Stateful built-ins (``ta.ema`` ...) keep per-call-site state, matching
  Pine's "one series per call site" semantics.
* Strategy orders emitted on bar *i* fill at bar *i+1*'s open in historical
  mode (the TradingView default). In realtime mode the caller fills them at
  the confirmed close instead, because a market order sent at bar close fills
  at the prevailing price, not a full bar later.
* ``na`` is a first-class value that propagates through arithmetic and
  comparisons and is falsy in conditions.

The interpreter is a plain AST walker. It never touches Python's ``eval`` or
``exec`` and the only functions Pine code can call are the whitelisted
built-ins in this module.
"""

from dataclasses import dataclass, field

from pine import ast_nodes as ast
from pine.builtins import SUPPORTED_CONSTANTS
from pine.errors import KIND_RUNTIME, PineError

# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


class _Na:
    """The Pine ``na`` value."""

    _instance: "_Na | None" = None

    def __new__(cls) -> "_Na":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "na"

    def __bool__(self) -> bool:
        return False


NA = _Na()


def is_na(value) -> bool:
    return value is NA


def _num(value, node: ast.Node, what: str = "operand"):
    if is_na(value):
        return NA
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise PineError(
        kind=KIND_RUNTIME,
        line=node.line,
        column=node.column,
        message=f"Expected a number for {what}, got {type(value).__name__}",
    )


def _truthy(value, node: ast.Node) -> bool:
    """Pine condition semantics: na is falsy, values must be boolean."""
    if is_na(value):
        return False
    if isinstance(value, bool):
        return value
    raise PineError(
        kind=KIND_RUNTIME,
        line=node.line,
        column=node.column,
        message="Condition must be a boolean value",
    )


# Direction sentinels for strategy.entry()
LONG_DIRECTION = "long"
SHORT_DIRECTION = "short"

COLORS = {
    "aqua": "#00bcd4",
    "black": "#263238",
    "blue": "#2962ff",
    "fuchsia": "#e040fb",
    "gray": "#787b86",
    "green": "#089981",
    "lime": "#00e676",
    "maroon": "#8b3a3a",
    "navy": "#311b92",
    "olive": "#808000",
    "orange": "#ff9800",
    "purple": "#9c27b0",
    "red": "#ef5350",
    "silver": "#b2b5be",
    "teal": "#00897b",
    "white": "#d1d4dc",
    "yellow": "#ffd144",
}

SHAPE_STYLES = {
    "shape.triangleup": "triangleup",
    "shape.triangledown": "triangledown",
    "shape.arrowup": "arrowup",
    "shape.arrowdown": "arrowdown",
    "shape.circle": "circle",
    "shape.square": "square",
    "shape.cross": "cross",
    "shape.xcross": "xcross",
    "shape.flag": "flag",
    "shape.labelup": "labelup",
    "shape.labeldown": "labeldown",
}

LOCATIONS = {
    "location.abovebar": "above",
    "location.belowbar": "below",
    "location.top": "top",
    "location.bottom": "bottom",
    "location.absolute": "absolute",
}

ALERT_FREQ = {
    "alert.freq_once_per_bar": "once_per_bar",
    "alert.freq_once_per_bar_close": "once_per_bar_close",
    "alert.freq_per_bar": "per_bar",
    "alert.freq_all": "all",
}


# ---------------------------------------------------------------------------
# Bar / config / results
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    """One candle. ``time`` is epoch milliseconds, ``index`` the 0-based bar number."""

    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    time: int = 0
    index: int = 0


@dataclass
class RuntimeConfig:
    """Strategy-level configuration shared by the runtime and the simulator."""

    default_qty: float = 1.0
    initial_capital: float = 100000.0
    commission_pct: float = 0.0  # percent of trade value per fill
    slippage_ticks: float = 0.0  # ticks of adverse slippage per fill
    tick_size: float = 0.05
    long_enabled: bool = True
    short_enabled: bool = True


@dataclass
class PlotOutput:
    """A plot() series aligned with the processed bars."""

    id: str
    title: str
    color: str | None
    values: list = field(default_factory=list)  # per-bar value (may be NA)


@dataclass
class ShapeOutput:
    """One plotshape() marker on a bar where the condition was true."""

    time: int
    bar_index: int
    title: str
    style: str
    location: str
    color: str | None
    text: str


@dataclass
class HLineOutput:
    """A horizontal level drawn by hline()."""

    price: float
    title: str
    color: str | None


@dataclass
class SignalOutput:
    """A strategy order intent emitted on one bar.

    This is *intent*, never an order: the service layer converts it into the
    normalized signal payload that flows into OpenAlgo's execution pipeline.
    """

    signal: str  # BUY | SELL
    kind: str  # entry | close | exit_stop | exit_limit
    order_id: str
    qty: float
    bar_index: int
    bar_time: int
    price: float  # signal-bar close
    comment: str


@dataclass
class TradeOutput:
    """A completed round trip produced by the internal trade simulator."""

    entry_id: str
    direction: str  # long | short
    qty: float
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    pnl: float
    exit_reason: str  # close | opposite | stop | limit


@dataclass
class AlertOutput:
    """An internal alert fired by alertcondition() or alert()."""

    kind: str  # condition | call
    title: str
    message: str
    bar_index: int
    bar_time: int


@dataclass
class RuntimeResult:
    """Everything produced by running the script over a series of bars."""

    title: str = ""
    kind: str = "indicator"  # indicator | strategy
    overlay: bool = True
    precision: int | None = None
    plots: list = field(default_factory=list)
    shapes: list = field(default_factory=list)
    hlines: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    position_size: float = 0.0
    position_avg_price: float = 0.0
    bars_processed: int = 0


# ---------------------------------------------------------------------------
# Stateful ta functions (one instance per call site)
# ---------------------------------------------------------------------------


class _WindowFunc:
    """Base for windowed functions: na until the window has ``length`` values."""

    def __init__(self, length: int) -> None:
        self.length = max(1, int(length))
        self.window: list = []

    def push(self, value) -> None:
        if not is_na(value):
            self.window.append(value)
            if len(self.window) > self.length:
                self.window.pop(0)

    def compute(self) -> float:
        if len(self.window) < self.length:
            return NA
        return self._reduce(self.window)

    def _reduce(self, window: list) -> float:  # pragma: no cover - overridden
        raise NotImplementedError


class _Sma(_WindowFunc):
    def _reduce(self, window: list) -> float:
        return sum(window) / len(window)


class _Wma(_WindowFunc):
    def _reduce(self, window: list) -> float:
        n = len(window)
        denom = n * (n + 1) / 2
        return sum(v * (i + 1) for i, v in enumerate(window)) / denom


class _Stdev(_WindowFunc):
    def __init__(self, length: int, population: bool = True) -> None:
        super().__init__(length)
        self.population = population

    def _reduce(self, window: list) -> float:
        n = len(window)
        mean = sum(window) / n
        var = sum((v - mean) ** 2 for v in window) / (n if self.population else n - 1)
        return var**0.5


class _Recursive:
    """Base for recursively smoothed series seeded with an SMA."""

    def __init__(self, length: int) -> None:
        self.length = max(1, int(length))
        self.seed: list = []
        self.value = NA

    def push(self, v: float) -> None:
        if is_na(v):
            return
        if is_na(self.value):
            self.seed.append(v)
            if len(self.seed) == self.length:
                self.value = sum(self.seed) / self.length
        else:
            self.value = self._step(self.value, v)

    def _step(self, prev: float, v: float) -> float:  # pragma: no cover - overridden
        raise NotImplementedError


class _Ema(_Recursive):
    def __init__(self, length: int) -> None:
        super().__init__(length)
        self.alpha = 2.0 / (self.length + 1)

    def _step(self, prev: float, v: float) -> float:
        return self.alpha * v + (1 - self.alpha) * prev


class _Rma(_Recursive):
    def __init__(self, length: int) -> None:
        super().__init__(length)
        self.alpha = 1.0 / self.length

    def _step(self, prev: float, v: float) -> float:
        return self.alpha * v + (1 - self.alpha) * prev


class _Rsi:
    """Wilder RSI: rma(gain) / (rma(gain) + rma(loss))."""

    def __init__(self, length: int) -> None:
        self.length = max(1, int(length))
        self.avg_gain = _Rma(length)
        self.avg_loss = _Rma(length)
        self.prev = NA

    def push(self, value) -> float:
        if is_na(self.prev):
            self.prev = value
            return NA
        if is_na(value):
            return NA
        change = value - self.prev
        self.prev = value
        self.avg_gain.push(max(change, 0.0))
        self.avg_loss.push(max(-change, 0.0))
        gain, loss = self.avg_gain.value, self.avg_loss.value
        if is_na(gain) or is_na(loss):
            return NA
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)


class _Crossover:
    """Generic cross detector storing previous values of both series."""

    def __init__(self) -> None:
        self.prev_a = NA
        self.prev_b = NA

    def push(self, a, b) -> bool:
        result = self._detect(self.prev_a, self.prev_b, a, b)
        self.prev_a = a
        self.prev_b = b
        return result

    def _detect(self, prev_a, prev_b, a, b) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


class _CrossoverUp(_Crossover):
    def _detect(self, prev_a, prev_b, a, b) -> bool:
        if any(is_na(v) for v in (prev_a, prev_b, a, b)):
            return False
        return prev_a <= prev_b and a > b


class _CrossoverDown(_Crossover):
    def _detect(self, prev_a, prev_b, a, b) -> bool:
        if any(is_na(v) for v in (prev_a, prev_b, a, b)):
            return False
        return prev_a >= prev_b and a < b


class _CrossAny(_Crossover):
    def _detect(self, prev_a, prev_b, a, b) -> bool:
        if any(is_na(v) for v in (prev_a, prev_b, a, b)):
            return False
        return (prev_a <= prev_b and a > b) or (prev_a >= prev_b and a < b)


class _HighestLowest:
    def __init__(self, length: int | None, highest: bool) -> None:
        self.length = None if length is None else max(1, int(length))
        self.highest = highest
        self.window: list = []

    def push(self, value):
        if is_na(value):
            return NA
        self.window.append(value)
        if self.length is not None:
            if len(self.window) > self.length:
                self.window.pop(0)
            if len(self.window) < self.length:
                return NA
        return max(self.window) if self.highest else min(self.window)


class _Vwap:
    def __init__(self) -> None:
        self.day = None
        self.pv = 0.0
        self.vol = 0.0

    def push(self, bar: Bar):
        import datetime as _dt

        day = _dt.datetime.fromtimestamp(bar.time / 1000.0, tz=_dt.UTC).date()
        if day != self.day:
            self.day = day
            self.pv = 0.0
            self.vol = 0.0
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.pv += typical * (bar.volume or 0.0)
        self.vol += bar.volume or 0.0
        if self.vol == 0:
            return NA
        return self.pv / self.vol


# ---------------------------------------------------------------------------
# Strategy simulator
# ---------------------------------------------------------------------------


@dataclass
class _PendingOrder:
    order_id: str
    kind: str  # entry | close
    direction: str  # long | short (entries only)
    qty: float
    source: str  # entry | close | close_all
    comment: str
    from_entry: str | None = None  # entry id a close targets; None = any


@dataclass
class _OpenPosition:
    entry_id: str
    direction: str
    qty: float
    entry_price: float
    entry_time: int
    entry_index: int
    stop: float | None = None
    limit: float | None = None
    exit_ids: set = field(default_factory=set)


class StrategySim:
    """Minimal TradingView-style broker simulator used by the runtime.

    Market orders (strategy.entry / strategy.close) fill at the NEXT bar's
    open. strategy.exit stop/limit orders fill intrabar against the bar's
    high/low, at the stop/limit price (or the open when the bar gaps past).
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.position = 0.0  # +qty long, -qty short
        self.avg_price = 0.0
        self.entries: dict[str, _OpenPosition] = {}
        self.pending: list[_PendingOrder] = []
        self.trades: list[TradeOutput] = []
        self.realized_pnl = 0.0
        self._seq = 0

    # -- order intake -----------------------------------------------------

    def next_id(self) -> int:
        self._seq += 1
        return self._seq

    def submit_entry(self, order_id: str, direction: str, qty: float, comment: str) -> None:
        if direction == "long" and not self.config.long_enabled:
            return
        if direction == "short" and not self.config.short_enabled:
            return
        # A same-direction entry while already positioned in that direction is
        # a no-op in TradingView unless qty increases; keep the simple rule:
        # replace pending same-id order, otherwise queue the reversal/entry.
        self.pending = [p for p in self.pending if p.order_id != order_id]
        self.pending.append(
            _PendingOrder(order_id, "entry", direction, qty, "entry", comment)
        )

    def submit_close(self, order_id: str, comment: str, from_entry: str | None = None) -> None:
        self.pending.append(
            _PendingOrder(order_id, "close", "", 0.0, "close", comment, from_entry)
        )

    def set_exit(
        self,
        exit_id: str,
        from_entry: str | None,
        stop: float | None,
        limit: float | None,
    ) -> None:
        """Attach stop/limit levels to matching open entries."""
        targets = [
            e
            for e in self.entries.values()
            if from_entry is None or e.entry_id == from_entry
        ]
        for entry in targets:
            entry.stop = stop
            entry.limit = limit
            entry.exit_ids.add(exit_id)

    def cancel(self, order_id: str) -> None:
        self.pending = [p for p in self.pending if p.order_id != order_id]
        for entry in self.entries.values():
            entry.exit_ids.discard(order_id)
            if not entry.exit_ids:
                entry.stop = None
                entry.limit = None

    def cancel_all(self) -> None:
        self.pending = []
        for entry in self.entries.values():
            entry.exit_ids = set()
            entry.stop = None
            entry.limit = None

    # -- bar processing ----------------------------------------------------

    def on_bar_open(self, bar: Bar) -> list[dict]:
        """Fill pending market orders at this bar's open; check exits."""
        fills: list[dict] = []
        price = self._slipped(bar.open, "market")

        for order in self.pending:
            if order.kind == "entry":
                fills.extend(self._fill_entry(order, price, bar))
            else:
                fills.extend(self._fill_close(order, price, bar, "close"))
        self.pending = []

        # Stop / limit exits for open entries, intrabar.
        for entry in list(self.entries.values()):
            stop_fill = None
            limit_fill = None
            if entry.stop is not None and bar.low <= entry.stop:
                stop_fill = min(bar.open, entry.stop) if bar.open < entry.stop else entry.stop
            if entry.limit is not None and bar.high >= entry.limit:
                limit_fill = max(bar.open, entry.limit) if bar.open > entry.limit else entry.limit
            if stop_fill is not None and limit_fill is not None:
                # Conservative: assume the stop hit first.
                stop_fill, limit_fill = stop_fill, None
            if stop_fill is not None:
                fills.extend(
                    self._close_position(entry, self._slipped(stop_fill, "stop"), bar, "stop")
                )
            elif limit_fill is not None:
                fills.extend(
                    self._close_position(entry, self._slipped(limit_fill, "limit"), bar, "limit")
                )

        return fills

    def fill_now(self, price: float, bar: Bar) -> list[dict]:
        """Realtime mode: fill pending market orders immediately at bar close."""
        fills: list[dict] = []
        for order in self.pending:
            if order.kind == "entry":
                fills.extend(self._fill_entry(order, price, bar))
            else:
                fills.extend(self._fill_close(order, price, bar, "close"))
        self.pending = []
        return fills

    # -- internals ----------------------------------------------------------

    def _fill_entry(self, order: _PendingOrder, price: float, bar: Bar) -> list[dict]:
        fills: list[dict] = []
        # Close opposite entries first (reversal).
        for entry in list(self.entries.values()):
            if entry.direction != order.direction:
                fills.extend(
                    self._close_position(entry, price, bar, "opposite")
                )
        qty = order.qty if order.qty > 0 else self.config.default_qty
        if qty <= 0:
            return fills
        if order.direction in self.entries:
            # Same-direction re-entry: merge quantities at the weighted average.
            existing = self.entries[order.direction]
            total = existing.qty + qty
            existing.entry_price = (
                existing.entry_price * existing.qty + price * qty
            ) / total
            existing.qty = total
            self.avg_price = existing.entry_price
            self.position = total if order.direction == "long" else -total
            return fills
        entry = _OpenPosition(
            entry_id=order.order_id,
            direction=order.direction,
            qty=qty,
            entry_price=price,
            entry_time=bar.time,
            entry_index=bar.index,
        )
        self.entries[order.direction] = entry
        total_before = abs(self.position)
        if total_before == 0:
            self.avg_price = price
            self.position = qty if order.direction == "long" else -qty
        else:
            self.avg_price = (self.avg_price * total_before + price * qty) / (total_before + qty)
            self.position = (total_before + qty) * (1 if order.direction == "long" else -1)
        return fills

    def _fill_close(
        self, order: _PendingOrder, price: float, bar: Bar, reason: str
    ) -> list[dict]:
        fills: list[dict] = []
        for entry in list(self.entries.values()):
            if order.source == "close" and order.from_entry not in (None, entry.entry_id):
                continue
            fills.extend(self._close_position(entry, price, bar, reason))
        return fills

    def _close_position(self, entry: _OpenPosition, price: float, bar: Bar, reason: str) -> list[dict]:
        direction = 1 if entry.direction == "long" else -1
        pnl = (price - entry.entry_price) * entry.qty * direction
        self.realized_pnl += pnl
        self.trades.append(
            TradeOutput(
                entry_id=entry.entry_id,
                direction=entry.direction,
                qty=entry.qty,
                entry_time=entry.entry_time,
                entry_price=entry.entry_price,
                exit_time=bar.time,
                exit_price=price,
                pnl=pnl,
                exit_reason=reason,
            )
        )
        self.entries.pop(entry.direction, None)
        # Recompute aggregate position from remaining entries.
        remaining = 0.0
        cost = 0.0
        for pos in self.entries.values():
            signed = pos.qty if pos.direction == "long" else -pos.qty
            remaining += signed
            cost += pos.entry_price * pos.qty
        self.position = remaining
        self.avg_price = (cost / abs(remaining)) if remaining != 0 and cost else 0.0
        return []

    def _slipped(self, price: float, what: str) -> float:
        if self.config.slippage_ticks and self.config.slippage_ticks > 0:
            return price - self.config.slippage_ticks * self.config.tick_size
        return price


# ---------------------------------------------------------------------------
# The interpreter
# ---------------------------------------------------------------------------


class PineRuntime:
    """Interprets a validated Pine AST one bar at a time."""

    def __init__(
        self,
        script: ast.Script,
        inputs: dict | None = None,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.script = script
        self.inputs = dict(inputs or {})
        self.config = config or RuntimeConfig()
        self.sim = StrategySim(self.config)

        # Outputs
        self.meta: dict = {}
        self.plots: list[PlotOutput] = []
        self.shapes: list[ShapeOutput] = []
        self.hlines: list[HLineOutput] = []
        self.signals: list[SignalOutput] = []
        self.alerts: list[AlertOutput] = []

        # State
        self._vars: dict[str, object] = {}
        self._var_initialized: dict[str, bool] = {}  # for `var` declarations
        self._var_persistent: set[str] = set()
        self._var_history: dict[str, list] = {}
        self._functions: dict[str, ast.FunctionDef] = {}
        self._call_state: dict[int, object] = {}  # id(AST node) -> stateful state
        self._call_history: dict[int, list] = {}  # id(AST node) -> per-bar values
        self._alert_fired: set[int] = set()  # id(call site) for once-per-bar alert()
        self.bars: list[Bar] = []
        self.bar: Bar | None = None

        self._extract_header_meta()
        for statement in self.script.statements:
            if isinstance(statement, ast.FunctionDef):
                self._functions[statement.name] = statement
        self._annotate_input_names()

    # ------------------------------------------------------------------
    # Metadata + inputs
    # ------------------------------------------------------------------

    def _extract_header_meta(self) -> None:
        first = self.script.statements[0]
        call = first.expression
        self.meta = {
            "title": call.args[0].value if call.args else "Untitled",
            "kind": "strategy" if call.name == "strategy" else "indicator",
            "overlay": True,
        }
        overlay = call.kwargs.get("overlay")
        if overlay is not None and isinstance(overlay, ast.BoolLiteral):
            self.meta["overlay"] = overlay.value
        precision = call.kwargs.get("precision")
        if precision is not None and isinstance(precision, ast.NumberLiteral):
            self.meta["precision"] = int(precision.value)
        initial_capital = call.kwargs.get("initial_capital")
        if initial_capital is not None and isinstance(initial_capital, ast.NumberLiteral):
            self.config.initial_capital = initial_capital.value
        default_qty = call.kwargs.get("default_qty")
        if default_qty is not None and isinstance(default_qty, ast.NumberLiteral):
            self.config.default_qty = default_qty.value
        commission = call.kwargs.get("commission_type")
        _ = commission  # commission handled via RuntimeConfig only

    def _annotate_input_names(self) -> None:
        """Bind each input.* call site to the variable it initialises.

        The service layer persists inputs keyed by this name so a saved
        strategy configuration maps back onto the same call sites after a
        restart.
        """

        def visit(node) -> None:
            if isinstance(node, ast.Declaration) and isinstance(node.value, ast.Call):
                if node.value.name in (
                    "input",
                    "input.int",
                    "input.float",
                    "input.bool",
                    "input.string",
                ):
                    # dataclass instances allow attribute assignment (no slots)
                    node.value._input_name = node.name
            for child in _child_nodes(node):
                visit(child)

        for statement in self.script.statements:
            visit(statement)

    def extract_inputs(self) -> list[dict]:
        """Pull input definitions from the AST (compile-time, no evaluation).

        Only literal defaults are accepted; the validator enforces this via
        the runtime raising on non-literal defaults at first use, and the
        extraction here simply skips anything non-literal.
        """
        definitions: list[dict] = []

        def visit(node) -> None:
            if isinstance(node, ast.Declaration) and isinstance(node.value, ast.Call):
                _visit_input_call(node.value, node.name, definitions)
            for child in _child_nodes(node):
                visit(child)

        for statement in self.script.statements:
            visit(statement)
        return definitions

    # ------------------------------------------------------------------
    # Bar processing
    # ------------------------------------------------------------------

    @property
    def bar_index(self) -> int:
        return len(self.bars) - 1

    def process_bar(self, bar: Bar, realtime: bool = False) -> list[SignalOutput]:
        """Process one confirmed bar and return the signals it produced.

        ``realtime=True`` fills any pending market orders at this bar's close
        (immediate market execution) instead of leaving them for the next
        bar's open.
        """
        bar.index = len(self.bars)
        self.bars.append(bar)
        self.bar = bar
        self._alert_fired.clear()

        # Market orders emitted on the previous bar fill at this bar's open -
        # TradingView's default fill model, applied identically in historical
        # and realtime mode. (In realtime, pending orders are usually already
        # cleared by the previous bar's fill_now, so this is a no-op then.)
        self._collect_fills(self.sim.on_bar_open(bar))

        # Execute the script body for this bar.
        pending_before = {id(o) for o in self.sim.pending}
        for statement in self.script.statements:
            self._exec_statement(statement)

        # Orders the script just emitted on this bar are signal intents.
        emitted: list[SignalOutput] = []
        for order in self.sim.pending:
            if id(order) in pending_before:
                continue
            emitted.append(self._order_to_signal(order, bar))
        self.signals.extend(emitted)

        if realtime:
            # At a confirmed close a market order executes at the prevailing
            # price, so fill immediately rather than waiting a full bar.
            self._collect_fills(self.sim.fill_now(bar.close, bar))

        # Close out per-bar variable history.
        for name in list(self._vars):
            self._var_history.setdefault(name, []).append(self._vars[name])

        return emitted

    def discard_pending(self) -> None:
        """Drop unfilled simulator orders left over from a historical warmup.

        Orders generated on historical bars are display artefacts of the
        backtest semantics (next-bar-open fill); they must never become live
        orders when the strategy starts.
        """
        if self.sim.pending:
            self.sim.pending = []

    def _collect_fills(self, fills: list[dict]) -> None:
        # Fills are reflected in sim state; nothing extra needed here yet.
        _ = fills

    def _order_to_signal(self, order: _PendingOrder, bar: Bar) -> SignalOutput:
        if order.kind == "entry":
            signal = "BUY" if order.direction == "long" else "SELL"
            kind = "entry"
            qty = order.qty if order.qty > 0 else self.config.default_qty
        else:
            # Closing: direction depends on current aggregate position.
            signal = "SELL" if self.sim.position > 0 else "BUY"
            kind = "close"
            qty = abs(self.sim.position)
        return SignalOutput(
            signal=signal,
            kind=kind,
            order_id=order.order_id,
            qty=qty,
            bar_index=bar.index,
            bar_time=bar.time,
            price=bar.close,
            comment=order.comment,
        )

    # ------------------------------------------------------------------
    # Statement execution
    # ------------------------------------------------------------------

    def _exec_statement(self, statement: ast.Node, scope: dict | None = None) -> None:
        if isinstance(statement, ast.Declaration):
            persistent = statement.persistent
            if persistent and self._var_initialized.get(statement.name):
                return  # `var`: initialize once, keep value on later bars
            value = self._eval(statement.value, scope)
            if scope is not None:
                scope[statement.name] = value
            else:
                self._vars[statement.name] = value
                self._var_initialized[statement.name] = True
                if persistent:
                    self._var_persistent.add(statement.name)
            return

        if isinstance(statement, ast.Reassignment):
            value = self._eval(statement.value, scope)
            target = scope if scope is not None else self._vars
            if statement.name not in target:
                raise PineError(
                    kind=KIND_RUNTIME,
                    line=statement.line,
                    column=statement.column,
                    message=f"Cannot reassign undeclared variable '{statement.name}'",
                )
            target[statement.name] = value
            return

        if isinstance(statement, ast.IfStatement):
            for branch in statement.branches:
                condition = self._eval(branch.condition, scope)
                if _truthy(condition, branch.condition):
                    block_scope: dict = dict(scope) if scope is not None else {}
                    for inner in branch.block:
                        self._exec_statement(inner, block_scope)
                    self._promote_block_scope(block_scope, scope)
                    return
            if statement.else_block is not None:
                block_scope = dict(scope) if scope is not None else {}
                for inner in statement.else_block:
                    self._exec_statement(inner, block_scope)
                self._promote_block_scope(block_scope, scope)
            return

        if isinstance(statement, ast.ExpressionStatement):
            self._eval(statement.expression, scope)
            return

        if isinstance(statement, ast.FunctionDef):
            return  # registered at construction

        raise PineError(
            kind=KIND_RUNTIME,
            line=statement.line,
            column=statement.column,
            message="Unsupported statement",
        )

    def _promote_block_scope(self, block_scope: dict, outer: dict | None) -> None:
        """If-block semantics: reassignments to outer variables persist.

        New declarations stay local; reassignments of names that already
        existed in the outer scope are written back.
        """
        if outer is None:
            for name, value in block_scope.items():
                if name in self._vars:
                    self._vars[name] = value
            return
        for name, value in block_scope.items():
            if name in outer:
                outer[name] = value

    # ------------------------------------------------------------------
    # Expression evaluation
    # ------------------------------------------------------------------

    def _eval(self, expr: ast.Expr | None, scope: dict | None = None):
        if expr is None:
            return NA

        if isinstance(expr, ast.NumberLiteral):
            return expr.value

        if isinstance(expr, ast.StringLiteral):
            return expr.value

        if isinstance(expr, ast.BoolLiteral):
            return expr.value

        if isinstance(expr, ast.NaLiteral):
            return NA

        if isinstance(expr, ast.VarRef):
            return self._eval_ref(expr, scope)

        if isinstance(expr, ast.BinaryOp):
            return self._eval_binary(expr, scope)

        if isinstance(expr, ast.UnaryOp):
            return self._eval_unary(expr, scope)

        if isinstance(expr, ast.TernaryOp):
            condition = self._eval(expr.condition, scope)
            if is_na(condition):
                return NA
            return (
                self._eval(expr.then_expr, scope)
                if _truthy(condition, expr.condition)
                else self._eval(expr.else_expr, scope)
            )

        if isinstance(expr, ast.HistoryRef):
            return self._eval_history(expr, scope)

        if isinstance(expr, ast.Call):
            return self._eval_call(expr, scope)

        raise PineError(
            kind=KIND_RUNTIME,
            line=expr.line,
            column=expr.column,
            message="Unsupported expression",
        )

    def _eval_ref(self, ref: ast.VarRef, scope: dict | None):
        name = ref.name

        # Block-local / function scope first.
        if scope is not None and name in scope:
            return scope[name]
        if name in self._vars:
            return self._vars[name]
        if name in self._functions:
            return self._functions[name]

        # Built-in series.
        bar = self.bar
        if bar is None:
            return NA
        series_map = {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "time": float(bar.time),
            "bar_index": float(bar.index),
            "hl2": (bar.high + bar.low) / 2.0,
            "hlc3": (bar.high + bar.low + bar.close) / 3.0,
            "ohlc4": (bar.open + bar.high + bar.low + bar.close) / 4.0,
        }
        if name in series_map:
            return series_map[name]

        # Namespaced constants.
        constant = SUPPORTED_CONSTANTS.get(name)
        if constant == "long":
            return LONG_DIRECTION
        if constant == "short":
            return SHORT_DIRECTION
        if constant == "position_size":
            return self.sim.position
        if constant == "position_avg_price":
            return self.sim.avg_price
        if constant == "equity":
            return self.config.initial_capital + self.sim.realized_pnl
        if constant == "closedtrades":
            return float(len(self.sim.trades))
        if constant == "opentrades":
            return float(len(self.sim.entries))

        if name in SHAPE_STYLES:
            return SHAPE_STYLES[name]
        if name in LOCATIONS:
            return LOCATIONS[name]
        if name in ALERT_FREQ:
            return ALERT_FREQ[name]
        if name.startswith("color."):
            return name.split(".", 1)[1]
        if name.startswith("size."):
            return name.split(".", 1)[1]
        if name == "plot.style_line":
            return "line"
        if name == "plot.style_histogram":
            return "histogram"
        if name == "plot.style_columns":
            return "columns"
        if name == "plot.style_area":
            return "area"

        raise PineError(
            kind=KIND_RUNTIME,
            line=ref.line,
            column=ref.column,
            message=f"Unknown identifier '{name}'",
        )

    def _eval_binary(self, expr: ast.BinaryOp, scope: dict | None):
        op = expr.op
        left = self._eval(expr.left, scope)
        right = self._eval(expr.right, scope)

        if op == "and":
            if left is False or right is False:
                return False
            if is_na(left) or is_na(right):
                return NA
            return bool(left) and bool(right)
        if op == "or":
            if left is True or right is True:
                return True
            if is_na(left) or is_na(right):
                return NA
            return bool(left) or bool(right)

        if is_na(left) or is_na(right):
            if op in ("==", "!=", "<", ">", "<=", ">="):
                return NA
            return NA

        if op in ("==", "!=", "<", ">", "<=", ">="):
            if isinstance(left, str) or isinstance(right, str):
                if op == "==":
                    return left == right
                if op == "!=":
                    return left != right
                raise PineError(
                    kind=KIND_RUNTIME,
                    line=expr.line,
                    column=expr.column,
                    message="Strings only support == and != comparisons",
                )
            lf, rf = _num(left, expr), _num(right, expr)
            if is_na(lf) or is_na(rf):
                return NA
            return {
                "==": lf == rf,
                "!=": lf != rf,
                "<": lf < rf,
                ">": lf > rf,
                "<=": lf <= rf,
                ">=": lf >= rf,
            }[op]

        lf, rf = _num(left, expr), _num(right, expr)
        if is_na(lf) or is_na(rf):
            return NA
        if op == "+":
            return lf + rf
        if op == "-":
            return lf - rf
        if op == "*":
            return lf * rf
        if op == "/":
            if rf == 0:
                return NA
            return lf / rf
        if op == "%":
            if rf == 0:
                return NA
            return lf % rf

        raise PineError(
            kind=KIND_RUNTIME,
            line=expr.line,
            column=expr.column,
            message=f"Unsupported operator '{op}'",
        )

    def _eval_unary(self, expr: ast.UnaryOp, scope: dict | None):
        value = self._eval(expr.operand, scope)
        if expr.op == "not":
            if is_na(value):
                return NA
            if not isinstance(value, bool):
                raise PineError(
                    kind=KIND_RUNTIME,
                    line=expr.line,
                    column=expr.column,
                    message="'not' requires a boolean operand",
                )
            return not value
        if expr.op == "-":
            number = _num(value, expr)
            return NA if is_na(number) else -number
        raise PineError(
            kind=KIND_RUNTIME,
            line=expr.line,
            column=expr.column,
            message=f"Unsupported unary operator '{expr.op}'",
        )

    def _eval_history(self, expr: ast.HistoryRef, scope: dict | None):
        """History access: maintain per-node history of the base expression."""
        node_key = id(expr)
        history = self._call_history.setdefault(node_key, [])
        bar = self.bar
        if bar is None:
            return NA

        # Append once per bar; a node evaluated twice on the same bar (inside
        # conditionals) must not skew the series alignment.
        if len(history) <= bar.index:
            current = self._eval(expr.base, scope)
            history.append(current)

        offset_value = self._eval(expr.offset, scope)
        offset = _num(offset_value, expr.offset)
        if is_na(offset):
            return NA
        offset = int(offset)
        if offset < 0:
            raise PineError(
                kind=KIND_RUNTIME,
                line=expr.line,
                column=expr.column,
                message="History offset must be >= 0",
            )
        idx = len(history) - 1 - offset
        if idx < 0:
            return NA
        return history[idx]

    # ------------------------------------------------------------------
    # Call evaluation / built-in dispatch
    # ------------------------------------------------------------------

    def _eval_call(self, call: ast.Call, scope: dict | None):
        name = call.name

        # User-defined function.
        if name in self._functions and name not in (
            "indicator",
            "strategy",
        ):
            return self._call_user_function(call, scope)

        handler = self._BUILTINS.get(name)
        if handler is None:
            raise PineError(
                kind=KIND_RUNTIME,
                line=call.line,
                column=call.column,
                message=f"Unknown function '{name}'",
            )
        return handler(self, call, scope)

    def _call_user_function(self, call: ast.Call, scope: dict | None):
        func = self._functions[call.name]
        if len(call.args) != len(func.params):
            raise PineError(
                kind=KIND_RUNTIME,
                line=call.line,
                column=call.column,
                message=(
                    f"{func.name}() expects {len(func.params)} argument(s), "
                    f"got {len(call.args)}"
                ),
            )
        frame: dict = {}
        for param, arg in zip(func.params, call.args, strict=False):
            frame[param] = self._eval(arg, scope)
        return self._eval(func.body, frame)

    def _args(self, call: ast.Call, scope: dict | None, count: int | None = None) -> list:
        values = [self._eval(arg, scope) for arg in call.args]
        return values

    def _arg(self, call: ast.Call, scope: dict | None, index: int, key: str, default=None):
        """Positional arg at ``index`` when present, else kwarg ``key``."""
        if len(call.args) > index:
            return self._eval(call.args[index], scope)
        if key in call.kwargs:
            return self._eval(call.kwargs[key], scope)
        return default

    def _kwarg(self, call: ast.Call, key: str, scope: dict | None = None, default=None):
        expr = call.kwargs.get(key)
        if expr is None:
            return default
        return self._eval(expr, scope)

    def _literal_kwarg(self, call: ast.Call, key: str):
        """Return a kwarg only when it is a literal (used at compile time too)."""
        expr = call.kwargs.get(key)
        if expr is None:
            return None
        if isinstance(expr, ast.StringLiteral):
            return expr.value
        if isinstance(expr, ast.BoolLiteral):
            return expr.value
        if isinstance(expr, ast.NumberLiteral):
            return expr.value
        return None

    # -- header -------------------------------------------------------------

    def _bi_indicator(self, call, scope):
        return NA  # metadata captured at construction

    _bi_strategy = _bi_indicator

    # -- inputs ---------------------------------------------------------------

    def _bi_input(self, call, scope):
        default = self._eval(call.args[0], scope) if call.args else NA
        return self._input_value(call, default)

    def _bi_input_int(self, call, scope):
        default = self._eval(call.args[0], scope) if call.args else NA
        value = self._input_value(call, default)
        if is_na(value):
            return NA
        return float(int(_num(value, call)))

    def _bi_input_float(self, call, scope):
        default = self._eval(call.args[0], scope) if call.args else NA
        return self._input_value(call, default)

    def _bi_input_bool(self, call, scope):
        default = self._eval(call.args[0], scope) if call.args else NA
        return self._input_value(call, default)

    def _bi_input_string(self, call, scope):
        default = self._eval(call.args[0], scope) if call.args else NA
        return self._input_value(call, default)

    def _input_value(self, call, default):
        # The input name key: prefer the declared variable name assigned at
        # compile time; fall back to the title kwarg.
        key = getattr(call, "_input_name", None) or self._literal_kwarg(call, "title") or "input"
        if key in self.inputs:
            provided = self.inputs[key]
            if isinstance(default, bool) and not isinstance(provided, bool):
                return str(provided).lower() in ("true", "1", "yes")
            if isinstance(default, (int, float)) and not isinstance(default, bool):
                try:
                    return float(provided)
                except (TypeError, ValueError):
                    return default
            return provided
        return default

    # -- na helpers -------------------------------------------------------------

    def _bi_na_test(self, call, scope):
        (value,) = self._args(call, scope)
        return is_na(value)

    def _bi_nz(self, call, scope):
        values = self._args(call, scope)
        replacement = values[1] if len(values) > 1 else 0.0
        return replacement if is_na(values[0]) else values[0]

    # -- math ---------------------------------------------------------------------

    def _bi_math_abs(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        return NA if is_na(number) else abs(number)

    def _bi_math_max(self, call, scope):
        values = [_num(v, call) for v in self._args(call, scope)]
        if any(is_na(v) for v in values):
            return NA
        return max(values)

    def _bi_math_min(self, call, scope):
        values = [_num(v, call) for v in self._args(call, scope)]
        if any(is_na(v) for v in values):
            return NA
        return min(values)

    def _bi_math_round(self, call, scope):
        values = self._args(call, scope)
        number = _num(values[0], call)
        if is_na(number):
            return NA
        precision = _num(values[1], call) if len(values) > 1 else 0.0
        if is_na(precision):
            return NA
        digits = int(precision)
        factor = 10**digits
        return float(round(number * factor) / factor)

    def _bi_math_floor(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        import math

        return NA if is_na(number) else float(math.floor(number))

    def _bi_math_ceil(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        import math

        return NA if is_na(number) else float(math.ceil(number))

    def _bi_math_pow(self, call, scope):
        base, exponent = self._args(call, scope)
        b, e = _num(base, call), _num(exponent, call)
        if is_na(b) or is_na(e):
            return NA
        return b**e

    def _bi_math_sqrt(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        if is_na(number):
            return NA
        if number < 0:
            return NA
        return number**0.5

    def _bi_math_log(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        import math

        if is_na(number) or number <= 0:
            return NA
        return math.log(number)

    def _bi_math_log10(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        import math

        if is_na(number) or number <= 0:
            return NA
        return math.log10(number)

    def _bi_math_exp(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        import math

        return NA if is_na(number) else math.exp(number)

    def _bi_math_sign(self, call, scope):
        (value,) = self._args(call, scope)
        number = _num(value, call)
        if is_na(number):
            return NA
        if number > 0:
            return 1.0
        if number < 0:
            return -1.0
        return 0.0

    def _bi_math_avg(self, call, scope):
        values = [_num(v, call) for v in self._args(call, scope)]
        if any(is_na(v) for v in values):
            return NA
        return sum(values) / len(values)

    def _bi_math_sum(self, call, scope):
        values = [_num(v, call) for v in self._args(call, scope)]
        if any(is_na(v) for v in values):
            return NA
        return sum(values)

    # -- ta ------------------------------------------------------------------------

    def _stateful(self, call: ast.Call, factory):
        key = id(call)
        if key not in self._call_state:
            self._call_state[key] = factory()
        return self._call_state[key]

    def _series_and_length(self, call: ast.Call, scope: dict | None, default_length: int = 14):
        """Resolve (source, length) for f(source, length) built-ins.

        Handles both positional (ta.ema(close, 9)) and keyword
        (ta.ema(close, length=9)) forms.
        """
        values = self._args(call, scope)
        source = values[0] if values else NA
        length = default_length
        if len(values) > 1:
            length_value = _num(values[1], call)
            length = 1 if is_na(length_value) else int(length_value)
        else:
            kw = self._kwarg(call, "length", scope)
            if kw is not None and not is_na(kw):
                length = int(_num(kw, call))
        if length <= 0:
            raise PineError(
                kind=KIND_RUNTIME,
                line=call.line,
                column=call.column,
                message="Length argument must be a positive integer",
            )
        return source, length

    def _length_arg(self, call, scope, index: int = 1) -> int:
        values = self._args(call, scope)
        if len(values) > index:
            length = _num(values[index], call)
            if is_na(length):
                raise PineError(
                    kind=KIND_RUNTIME,
                    line=call.line,
                    column=call.column,
                    message="Length argument cannot be na",
                )
            length = int(length)
            if length <= 0:
                raise PineError(
                    kind=KIND_RUNTIME,
                    line=call.line,
                    column=call.column,
                    message="Length argument must be a positive integer",
                )
            return length
        return 1

    def _bi_ta_sma(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Sma(length))
        state.push(source)
        return state.compute()

    def _bi_ta_ema(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Ema(length))
        value = _num(source, call)
        state.push(value)
        return state.value

    def _bi_ta_wma(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Wma(length))
        state.push(source)
        return state.compute()

    def _bi_ta_rma(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Rma(length))
        value = _num(source, call)
        state.push(value)
        return state.value

    def _bi_ta_rsi(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Rsi(length))
        return state.push(_num(source, call))

    def _bi_ta_tr(self, call, scope):
        bar = self.bar
        self._kwarg(call, "handle_na", scope, False)  # accepted; first bar uses H-L
        state = self._stateful(call, lambda: {"prev_close": None})
        prev = state["prev_close"]
        state["prev_close"] = bar.close
        if prev is None:
            return bar.high - bar.low
        return max(
            bar.high - bar.low,
            abs(bar.high - prev),
            abs(bar.low - prev),
        )

    def _bi_ta_atr(self, call, scope):
        # ta.atr(length): Wilder-smoothed (rma) true range.
        length = self._length_arg(call, scope, 0)
        state = self._stateful(call, lambda: (_Rma(length), {"prev_close": None}))
        rma, tr_state = state
        bar = self.bar
        if tr_state["prev_close"] is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - tr_state["prev_close"]),
                abs(bar.low - tr_state["prev_close"]),
            )
        tr_state["prev_close"] = bar.close
        rma.push(tr)
        return rma.value

    def _bi_ta_highest(self, call, scope):
        values = self._args(call, scope)
        source = values[0] if values else NA
        length_value = values[1] if len(values) > 1 else self._kwarg(call, "length", scope)
        if length_value is None or is_na(length_value):
            # ta.highest(source) with no length: highest of all bars so far.
            state = self._stateful(call, lambda: _HighestLowest(None, True))
        else:
            length = int(_num(length_value, call))
            state = self._stateful(call, lambda: _HighestLowest(length, True))
        return state.push(source)

    def _bi_ta_lowest(self, call, scope):
        values = self._args(call, scope)
        source = values[0] if values else NA
        length_value = values[1] if len(values) > 1 else self._kwarg(call, "length", scope)
        if length_value is None or is_na(length_value):
            state = self._stateful(call, lambda: _HighestLowest(None, False))
        else:
            length = int(_num(length_value, call))
            state = self._stateful(call, lambda: _HighestLowest(length, False))
        return state.push(source)

    def _bi_ta_crossover(self, call, scope):
        a, b = self._args(call, scope)
        state = self._stateful(call, _CrossoverUp)
        return state.push(a, b)

    def _bi_ta_crossunder(self, call, scope):
        a, b = self._args(call, scope)
        state = self._stateful(call, _CrossoverDown)
        return state.push(a, b)

    def _bi_ta_cross(self, call, scope):
        a, b = self._args(call, scope)
        state = self._stateful(call, _CrossAny)
        return state.push(a, b)

    def _bi_ta_change(self, call, scope):
        source, length = self._series_and_length(call, scope, default_length=1)
        state = self._stateful(call, lambda: [])
        state.append(source)
        idx = len(state) - 1 - length
        if idx < 0:
            return NA
        if is_na(state[idx]) or is_na(source):
            return NA
        return source - state[idx]

    def _bi_ta_mom(self, call, scope):
        return self._bi_ta_change(call, scope)

    def _bi_ta_stdev(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Stdev(length))
        state.push(source)
        return state.compute()

    def _bi_ta_variance(self, call, scope):
        source, length = self._series_and_length(call, scope)
        state = self._stateful(call, lambda: _Stdev(length))
        state.push(source)
        stdev = state.compute()
        return NA if is_na(stdev) else stdev * stdev

    def _bi_ta_vwap(self, call, scope):
        (source,) = self._args(call, scope)
        state = self._stateful(call, _Vwap)
        return state.push(self.bar)

    # -- plots ------------------------------------------------------------------------

    def _bi_plot(self, call, scope):
        values = self._args(call, scope)
        source = values[0] if values else NA
        key = id(call)
        if key not in self._call_state:
            color = self._resolve_color(call, scope)
            title = self._literal_kwarg(call, "title") or f"Plot {len(self.plots) + 1}"
            output = PlotOutput(id=f"plot-{key}", title=str(title), color=color)
            self._call_state[key] = output
            self.plots.append(output)
        output = self._call_state[key]
        output.values.append(source)
        return NA

    def _resolve_color(self, call, scope):
        """Resolve a color kwarg: literal color name or color.* reference."""
        color = self._kwarg(call, "color", scope)
        if color is None or is_na(color):
            return None
        if isinstance(color, str):
            return COLORS.get(color, color)
        return None

    def _bi_plotshape(self, call, scope):
        values = self._args(call, scope)
        condition = values[0] if values else NA
        if _truthy(condition, call):
            style = self._arg(call, scope, 2, "style")
            if style is None or is_na(style):
                style = "triangleup"
            # Runtime values from shape.* references are already mapped;
            # raw strings may arrive as literals.
            if isinstance(style, str) and style in SHAPE_STYLES:
                style = SHAPE_STYLES[style]
            location = self._kwarg(call, "location", scope)
            if location is None or is_na(location):
                location = "above"
            if isinstance(location, str) and location in LOCATIONS:
                location = LOCATIONS[location]
            color = self._resolve_color(call, scope)
            title = self._literal_kwarg(call, "title")
            if title is None and len(values) > 1 and isinstance(values[1], str):
                title = values[1]
            title = title or ""
            text = self._literal_kwarg(call, "text") or ""
            self.shapes.append(
                ShapeOutput(
                    time=self.bar.time,
                    bar_index=self.bar.index,
                    title=str(title),
                    style=str(style),
                    location=str(location),
                    color=color,
                    text=str(text),
                )
            )
        return NA

    def _bi_hline(self, call, scope):
        values = self._args(call, scope)
        price = values[0] if values else NA
        key = id(call)
        if key not in self._call_state and not is_na(price):
            color = self._resolve_color(call, scope)
            title = self._literal_kwarg(call, "title")
            if title is None and len(values) > 1 and isinstance(values[1], str):
                title = values[1]
            title = title or f"Level {len(self.hlines) + 1}"
            level = HLineOutput(price=float(_num(price, call)), title=str(title), color=color)
            self._call_state[key] = level
            self.hlines.append(level)
        return NA

    def _bi_bgcolor(self, call, scope):
        # Accepted and ignored (no visual impact yet); documented as such.
        self._args(call, scope)
        return NA

    def _bi_color_new(self, call, scope):
        (base, transparency) = self._args(call, scope)
        _ = transparency  # transparency is not rendered; documented limitation
        if isinstance(base, str):
            return COLORS.get(base, base)
        return base

    def _bi_color_rgb(self, call, scope):
        values = self._args(call, scope)
        r, g, b = (int(_num(v, call)) for v in values[:3])
        return f"#{r:02x}{g:02x}{b:02x}"

    # -- strategy orders ------------------------------------------------------------------

    def _bi_strategy_entry(self, call, scope):
        values = self._args(call, scope)
        order_id = str(values[0])
        direction = values[1] if len(values) > 1 else self._kwarg(call, "direction")
        if direction == LONG_DIRECTION:
            direction = "long"
        elif direction == SHORT_DIRECTION:
            direction = "short"
        if direction not in ("long", "short"):
            raise PineError(
                kind=KIND_RUNTIME,
                line=call.line,
                column=call.column,
                message="strategy.entry() direction must be strategy.long or strategy.short",
            )
        qty = self._kwarg(call, "qty", scope, 0.0)
        qty = _num(qty, call) if not is_na(qty) else 0.0
        comment = self._kwarg(call, "comment", scope, "") or ""
        self.sim.submit_entry(order_id, direction, float(qty), str(comment))
        return NA

    def _bi_strategy_close(self, call, scope):
        values = self._args(call, scope)
        entry_id = str(values[0])
        comment = self._kwarg(call, "comment", scope, "") or ""
        order_id = f"close-{entry_id}-{self.sim.next_id()}"
        self.sim.submit_close(order_id, str(comment), from_entry=entry_id)
        return NA

    def _bi_strategy_close_all(self, call, scope):
        comment = self._kwarg(call, "comment", scope, "") or ""
        order_id = f"close-all-{self.sim.next_id()}"
        self.sim.submit_close(order_id, str(comment), from_entry=None)
        # close-all matches every open entry
        self.sim.pending[-1].source = "close_all"
        return NA

    def _bi_strategy_exit(self, call, scope):
        values = self._args(call, scope)
        exit_id = str(values[0]) if values else "exit"
        from_entry = self._arg(call, scope, 1, "from_entry")
        if from_entry is None or is_na(from_entry):
            from_entry = None
        else:
            from_entry = str(from_entry)
        stop = self._kwarg(call, "stop", scope)
        limit = self._kwarg(call, "limit", scope)
        stop = None if stop is None or is_na(stop) else float(_num(stop, call))
        limit = None if limit is None or is_na(limit) else float(_num(limit, call))
        if stop is None and limit is None:
            return NA  # exits with only profit/loss ticks are not supported yet
        self.sim.set_exit(exit_id, from_entry, stop, limit)
        return NA

    def _bi_strategy_cancel(self, call, scope):
        (order_id,) = self._args(call, scope)
        self.sim.cancel(str(order_id))
        return NA

    def _bi_strategy_cancel_all(self, call, scope):
        self.sim.cancel_all()
        return NA

    # -- alerts ------------------------------------------------------------------------

    def _bi_alertcondition(self, call, scope):
        values = self._args(call, scope)
        condition = values[0] if values else NA
        if _truthy(condition, call):
            title = self._literal_kwarg(call, "title")
            if title is None and len(values) > 1 and isinstance(values[1], str):
                title = values[1]
            title = title or "Alert"
            message = self._literal_kwarg(call, "message")
            if message is None and len(values) > 2 and isinstance(values[2], str):
                message = values[2]
            message = message or str(title)
            self.alerts.append(
                AlertOutput(
                    kind="condition",
                    title=str(title),
                    message=str(message),
                    bar_index=self.bar.index,
                    bar_time=self.bar.time,
                )
            )
        return NA

    def _bi_alert(self, call, scope):
        key = id(call)
        values = self._args(call, scope)
        message = values[0] if values else NA
        freq = self._arg(call, scope, 1, "freq") or "once_per_bar"
        if key in self._alert_fired and freq in ("once_per_bar", "once_per_bar_close"):
            return NA
        self._alert_fired.add(key)
        self.alerts.append(
            AlertOutput(
                kind="call",
                title="alert",
                message=str(message),
                bar_index=self.bar.index,
                bar_time=self.bar.time,
            )
        )
        return NA

    _BUILTINS = {
        "indicator": _bi_indicator,
        "strategy": _bi_strategy,
        "input": _bi_input,
        "input.int": _bi_input_int,
        "input.float": _bi_input_float,
        "input.bool": _bi_input_bool,
        "input.string": _bi_input_string,
        "na": _bi_na_test,
        "nz": _bi_nz,
        "math.abs": _bi_math_abs,
        "math.max": _bi_math_max,
        "math.min": _bi_math_min,
        "math.round": _bi_math_round,
        "math.floor": _bi_math_floor,
        "math.ceil": _bi_math_ceil,
        "math.pow": _bi_math_pow,
        "math.sqrt": _bi_math_sqrt,
        "math.log": _bi_math_log,
        "math.log10": _bi_math_log10,
        "math.exp": _bi_math_exp,
        "math.sign": _bi_math_sign,
        "math.avg": _bi_math_avg,
        "math.sum": _bi_math_sum,
        "ta.sma": _bi_ta_sma,
        "ta.ema": _bi_ta_ema,
        "ta.wma": _bi_ta_wma,
        "ta.rma": _bi_ta_rma,
        "ta.rsi": _bi_ta_rsi,
        "ta.tr": _bi_ta_tr,
        "ta.atr": _bi_ta_atr,
        "ta.highest": _bi_ta_highest,
        "ta.lowest": _bi_ta_lowest,
        "ta.crossover": _bi_ta_crossover,
        "ta.crossunder": _bi_ta_crossunder,
        "ta.cross": _bi_ta_cross,
        "ta.change": _bi_ta_change,
        "ta.mom": _bi_ta_mom,
        "ta.stdev": _bi_ta_stdev,
        "ta.variance": _bi_ta_variance,
        "ta.vwap": _bi_ta_vwap,
        "plot": _bi_plot,
        "plotshape": _bi_plotshape,
        "hline": _bi_hline,
        "bgcolor": _bi_bgcolor,
        "color.new": _bi_color_new,
        "color.rgb": _bi_color_rgb,
        "strategy.entry": _bi_strategy_entry,
        "strategy.close": _bi_strategy_close,
        "strategy.close_all": _bi_strategy_close_all,
        "strategy.exit": _bi_strategy_exit,
        "strategy.cancel": _bi_strategy_cancel,
        "strategy.cancel_all": _bi_strategy_cancel_all,
        "alertcondition": _bi_alertcondition,
        "alert": _bi_alert,
    }

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def result(self) -> RuntimeResult:
        return RuntimeResult(
            title=self.meta.get("title", ""),
            kind=self.meta.get("kind", "indicator"),
            overlay=self.meta.get("overlay", True),
            precision=self.meta.get("precision"),
            plots=list(self.plots),
            shapes=list(self.shapes),
            hlines=list(self.hlines),
            signals=list(self.signals),
            trades=list(self.sim.trades),
            alerts=list(self.alerts),
            position_size=self.sim.position,
            position_avg_price=self.sim.avg_price,
            bars_processed=len(self.bars),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _child_nodes(node) -> list:
    """Direct AST children that can contain declarations or input calls."""
    children: list = []
    if isinstance(node, ast.Declaration):
        children.append(node.value)
    elif isinstance(node, ast.Reassignment):
        children.append(node.value)
    elif isinstance(node, ast.FunctionDef):
        children.append(node.body)
    elif isinstance(node, ast.IfStatement):
        for branch in node.branches:
            children.extend(branch.block)
        if node.else_block is not None:
            children.extend(node.else_block)
    elif isinstance(node, ast.ExpressionStatement):
        children.append(node.expression)
    elif isinstance(node, ast.IfBranch):
        children.extend(node.block)
    return children


def _visit_input_call(call: ast.Call, var_name: str, definitions: list) -> None:
    """Record an input definition when the call site is an input.* function."""
    input_fns = {
        "input": "float",
        "input.int": "int",
        "input.float": "float",
        "input.bool": "bool",
        "input.string": "string",
    }
    if call.name not in input_fns:
        return
    if not call.args:
        return
    default_expr = call.args[0]
    if not isinstance(
        default_expr, ast.NumberLiteral | ast.BoolLiteral | ast.StringLiteral
    ):
        return  # non-literal defaults are rejected by the validator contract
    default = default_expr.value
    if isinstance(default, float) and default_expr.value == int(default_expr.value):
        default = default  # keep as-is; JSON layer formats numbers

    def literal_kwarg(key: str):
        expr = call.kwargs.get(key)
        if isinstance(expr, ast.NumberLiteral):
            return expr.value
        if isinstance(expr, ast.StringLiteral):
            return expr.value
        return None

    definitions.append(
        {
            "name": var_name,
            "title": literal_kwarg("title") or var_name,
            "type": input_fns[call.name],
            "default": default,
            "minval": literal_kwarg("minval"),
            "maxval": literal_kwarg("maxval"),
            "step": literal_kwarg("step"),
        }
    )
