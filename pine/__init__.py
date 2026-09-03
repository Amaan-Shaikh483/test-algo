"""Pine Script engine for the OpenAlgo trading terminal.

A TradingView-compatible *subset* of Pine Script v5: lexer -> parser -> AST ->
semantic validation -> bar-by-bar interpreter. The runtime is deliberately
sandboxed: Pine code is never executed as Python (no eval/exec), can only call
the whitelisted built-ins registered in ``pine/builtins.py`` and has no access
to the filesystem, network, database or broker credentials.

One runtime powers every consumer (chart evaluation, backtesting, realtime
strategy execution) so the semantics stay identical across surfaces.
"""

from pine.compiler import CompileResult, compile_script
from pine.errors import PineError
from pine.runtime import Bar, PineRuntime, RuntimeResult

__all__ = [
    "Bar",
    "CompileResult",
    "PineError",
    "PineRuntime",
    "RuntimeResult",
    "compile_script",
]
