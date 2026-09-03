"""Front door of the Pine engine: source text -> validated, runnable program.

``compile_script`` runs the lexer, parser and semantic validator and returns a
``CompileResult`` carrying the AST, script metadata and input definitions.
Runtime construction stays with the caller because a compiled program can be
run against many bar series (chart evaluation, backtest, live strategy).
"""

from dataclasses import dataclass, field

from pine import ast_nodes as ast
from pine.builtins import SUPPORTED_BUILTINS
from pine.errors import PineError
from pine.parser import parse_script
from pine.validator import validate_script


@dataclass
class CompileResult:
    """Everything the compile step produces."""

    ok: bool
    script: ast.Script | None = None
    title: str = ""
    kind: str = "indicator"  # indicator | strategy
    overlay: bool = True
    inputs: list = field(default_factory=list)  # input definitions for the UI
    error: PineError | None = None

    def to_dict(self) -> dict:
        """JSON shape returned by the compile API."""
        if not self.ok:
            return {
                "status": "error",
                "error": self.error.to_dict() if self.error else None,
            }
        return {
            "status": "success",
            "title": self.title,
            "kind": self.kind,
            "overlay": self.overlay,
            "inputs": self.inputs,
            "supported": True,
        }


def compile_script(source: str) -> CompileResult:
    """Compile Pine source; never raises, failures land in the result."""
    try:
        script = parse_script(source)
        validate_script(script)
    except PineError as error:
        return CompileResult(ok=False, error=error)

    # Metadata from the header call (validated to exist and start with a
    # string literal).
    header = script.statements[0].expression
    title = header.args[0].value if header.args else "Untitled"
    kind = "strategy" if header.name == "strategy" else "indicator"
    overlay = True
    overlay_expr = header.kwargs.get("overlay")
    if isinstance(overlay_expr, ast.BoolLiteral):
        overlay = overlay_expr.value

    # Input definitions require a throwaway runtime for extraction.
    from pine.runtime import PineRuntime

    runtime = PineRuntime(script)
    inputs = runtime.extract_inputs()

    return CompileResult(
        ok=True,
        script=script,
        title=title,
        kind=kind,
        overlay=overlay,
        inputs=inputs,
    )


def supported_builtins() -> list[dict]:
    """The supported built-in list for documentation/UI."""
    return [
        {"name": name, "min_args": spec.min_args, "max_args": spec.max_args, "role": spec.role}
        for name, spec in sorted(SUPPORTED_BUILTINS.items())
    ]
