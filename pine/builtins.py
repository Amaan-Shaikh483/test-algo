"""Whitelisted Pine built-ins: the complete surface user code can touch.

This module is the security boundary between Pine scripts and the host
application. The validator rejects any name not listed here, and the runtime
only ever dispatches to implementations registered here. Nothing in the Pine
path reaches the filesystem, network, database or broker credentials.

Two registries matter:

* ``SUPPORTED_BUILTINS`` - callable/constant names the engine implements.
* ``UNSUPPORTED_FEATURES`` - real Pine v5 features that are *known* but not
  implemented. Referencing one fails with the spec-mandated
  ``Unsupported Pine feature: <name>()`` error instead of a vague unknown name.
"""

from dataclasses import dataclass


@dataclass
class BuiltinSpec:
    """Signature of a supported built-in call."""

    name: str
    min_args: int
    max_args: int  # -1 = unbounded
    # 'call' anywhere an expression is allowed; 'statement' only as a
    # standalone statement (plot, strategy.entry, alert ...).
    role: str = "call"


# ---------------------------------------------------------------------------
# Callable built-ins (expression role unless noted)
# ---------------------------------------------------------------------------

SUPPORTED_BUILTINS: dict[str, BuiltinSpec] = {}


def _register(spec: BuiltinSpec) -> None:
    SUPPORTED_BUILTINS[spec.name] = spec


# Technical analysis --------------------------------------------------------
_register(BuiltinSpec("ta.sma", 2, 2))
_register(BuiltinSpec("ta.ema", 2, 2))
_register(BuiltinSpec("ta.wma", 2, 2))
_register(BuiltinSpec("ta.rma", 2, 2))
_register(BuiltinSpec("ta.rsi", 2, 2))
_register(BuiltinSpec("ta.atr", 1, 2))
_register(BuiltinSpec("ta.tr", 0, 1))
_register(BuiltinSpec("ta.highest", 1, 2))
_register(BuiltinSpec("ta.lowest", 1, 2))
_register(BuiltinSpec("ta.crossover", 2, 2))
_register(BuiltinSpec("ta.crossunder", 2, 2))
_register(BuiltinSpec("ta.cross", 2, 2))
_register(BuiltinSpec("ta.change", 1, 2))
_register(BuiltinSpec("ta.mom", 2, 2))
_register(BuiltinSpec("ta.stdev", 2, 2))
_register(BuiltinSpec("ta.variance", 2, 2))
_register(BuiltinSpec("ta.vwap", 1, 1))

# Math ----------------------------------------------------------------------
_register(BuiltinSpec("math.abs", 1, 1))
_register(BuiltinSpec("math.max", 2, -1))
_register(BuiltinSpec("math.min", 2, -1))
_register(BuiltinSpec("math.round", 1, 2))
_register(BuiltinSpec("math.floor", 1, 1))
_register(BuiltinSpec("math.ceil", 1, 1))
_register(BuiltinSpec("math.pow", 2, 2))
_register(BuiltinSpec("math.sqrt", 1, 1))
_register(BuiltinSpec("math.log", 1, 1))
_register(BuiltinSpec("math.log10", 1, 1))
_register(BuiltinSpec("math.exp", 1, 1))
_register(BuiltinSpec("math.sign", 1, 1))
_register(BuiltinSpec("math.avg", 2, -1))
_register(BuiltinSpec("math.sum", 2, -1))

# na helpers ----------------------------------------------------------------
_register(BuiltinSpec("na", 1, 1))  # na(x) test
_register(BuiltinSpec("nz", 1, 2))

# Inputs --------------------------------------------------------------------
_register(BuiltinSpec("input", 1, 3))
_register(BuiltinSpec("input.int", 1, 6))
_register(BuiltinSpec("input.float", 1, 6))
_register(BuiltinSpec("input.bool", 1, 5))
_register(BuiltinSpec("input.string", 1, 5))

# Declaration calls ----------------------------------------------------------
_register(BuiltinSpec("indicator", 1, 10, role="call"))
_register(BuiltinSpec("strategy", 1, 20, role="call"))

# Plotting (statement role) ---------------------------------------------------
_register(BuiltinSpec("plot", 1, 8, role="statement"))
_register(BuiltinSpec("plotshape", 1, 12, role="statement"))
_register(BuiltinSpec("hline", 1, 4, role="statement"))
_register(BuiltinSpec("bgcolor", 1, 5, role="statement"))

# Strategy order calls (statement role) ----------------------------------------
_register(BuiltinSpec("strategy.entry", 1, 10, role="statement"))
_register(BuiltinSpec("strategy.close", 1, 6, role="statement"))
_register(BuiltinSpec("strategy.close_all", 0, 4, role="statement"))
_register(BuiltinSpec("strategy.exit", 1, 14, role="statement"))
_register(BuiltinSpec("strategy.cancel", 1, 3, role="statement"))
_register(BuiltinSpec("strategy.cancel_all", 0, 2, role="statement"))

# Alerts (statement role) ------------------------------------------------------
_register(BuiltinSpec("alertcondition", 1, 3, role="statement"))
_register(BuiltinSpec("alert", 1, 3, role="statement"))

# ---------------------------------------------------------------------------
# Read-only namespaced series/constants (referenced without calling)
# ---------------------------------------------------------------------------

SUPPORTED_CONSTANTS = {
    "strategy.long": "long",
    "strategy.short": "short",
    "strategy.position_size": "position_size",
    "strategy.position_avg_price": "position_avg_price",
    "strategy.equity": "equity",
    "strategy.closedtrades": "closedtrades",
    "strategy.opentrades": "opentrades",
    "plot.style_line": "line",
    "plot.style_stepline": "line",
    "plot.style_histogram": "histogram",
    "plot.style_columns": "columns",
    "plot.style_area": "area",
}

# Namespaced constant families accepted by the validator and resolved by the
# runtime: color.red, shape.triangleup, location.abovebar, size.small,
# alert.freq_once_per_bar ... (kept in sync with PineRuntime._eval_ref)
SUPPORTED_CONSTANT_PREFIXES = (
    "color.",
    "shape.",
    "location.",
    "size.",
    "alert.freq_",
    "plot.style_",
)

# ---------------------------------------------------------------------------
# Known Pine v5 features this engine deliberately does NOT implement.
# Referencing any of these is a compile error with an explicit message.
# ---------------------------------------------------------------------------

UNSUPPORTED_FEATURES: set[str] = {
    "request.security",
    "request.security_lower_tf",
    "request.economic",
    "request.quandl",
    "request.financial",
    "request.currency_rate",
    "request.dividends",
    "request.splits",
    "request.earnings",
    "request.symbols",
    "request.security_raw",
    "ta.pivothigh",
    "ta.pivotlow",
    "ta.percentrank",
    "ta.percentile_linear_interpolation",
    "ta.barssince",
    "ta.valuewhen",
    "ta.highestbars",
    "ta.lowestbars",
    "ta.macd",
    "ta.bb",
    "ta.stoch",
    "ta.supertrend",
    "ta.sar",
    "ta.linreg",
    "ta.correlation",
    "ta.cum",
    "ta.vwma",
    "ta.hma",
    "ta.swma",
    "ta.alma",
    "ta.rising",
    "ta.falling",
    "array.new",
    "array.new_int",
    "array.new_float",
    "array.new_bool",
    "array.new_string",
    "array.new_color",
    "array.new_line",
    "array.new_label",
    "array.new_box",
    "array.new_table",
    "array.push",
    "array.pop",
    "array.get",
    "array.set",
    "array.size",
    "matrix.new",
    "map.new",
    "label.new",
    "line.new",
    "box.new",
    "table.new",
    "plotchar",
    "plotcandle",
    "plotbar",
    "plotarrow",
    "barcolor",
    "fill",
    "color.new",
    "color.rgb",
    "str.format",
    "str.tostring",
    "str.tonumber",
    "timeframe.period",
    "timeframe.isintraday",
    "timeframe.isdwm",
    "syminfo.ticker",
    "syminfo.tickerid",
    "syminfo.prefix",
    "syminfo.currency",
    "session.ismarket",
    "session.ispremarket",
    "session.ispostmarket",
    "strategy.order",
    "strategy.risk.allow_entry_in",
    "strategy.risk.max_position_size",
    "strategy.risk.max_loss",
    "strategy.risk.max_intraday_loss",
    "strategy.risk.max_cons_loss_days",
    "strategy.risk.max_drawdown",
    "strategy.defaults",
    "runtime.error",
    "log.info",
    "log.warning",
    "log.error",
    "chart.right_visible_bar_time",
    "chart.left_visible_bar_time",
    "chart.is_standard",
    "input.color",
    "input.time",
    "input.source",
    "input.text_area",
    "input.session",
    "input.timeframe",
    "input.symbol",
    "input.price",
    "input.upper_timeframe",
    "ticker.new",
    "ticker.modify",
    "fixnan",
    "highest",
    "lowest",
    "security",
    "study",
}

# Namespaces that exist in real Pine. An unknown member of a known namespace
# gets "Unknown function in namespace X" rather than a generic unknown name,
# which keeps the error message actionable.
KNOWN_NAMESPACES = {"ta", "math", "strategy", "input", "request", "color", "str", "array"}


def is_supported_call(name: str) -> bool:
    return name in SUPPORTED_BUILTINS


def builtin_spec(name: str) -> BuiltinSpec | None:
    return SUPPORTED_BUILTINS.get(name)
