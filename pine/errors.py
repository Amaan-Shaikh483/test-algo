"""Structured errors for the Pine engine.

Every user-facing failure carries a line, a column and a machine-readable kind
so the editor can underline the exact token. The two kinds mirror the spec:
``compile_error`` for anything caught before execution and ``runtime_error``
for failures while a bar is being processed.
"""

from dataclasses import dataclass

KIND_COMPILE = "compile_error"
KIND_RUNTIME = "runtime_error"
KIND_UNSUPPORTED = "unsupported_feature"


@dataclass
class PineError(Exception):
    """A Pine engine error with source coordinates.

    Attributes:
        kind: One of ``compile_error``, ``runtime_error``, ``unsupported_feature``.
        line: 1-based line number.
        column: 1-based column number.
        message: Human-readable message shown in the editor console.
    """

    kind: str = KIND_COMPILE
    line: int = 0
    column: int = 0
    message: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Serialize for the JSON API / editor diagnostics.

        The wire format uses ``compile_error`` as the type for everything
        caught before execution (the spec's example shape), with ``category``
        carrying the finer-grained kind.
        """
        return {
            "type": KIND_COMPILE,
            "category": self.kind,
            "line": self.line,
            "column": self.column,
            "message": self.message,
        }

    def __str__(self) -> str:  # pragma: no cover - cosmetic only
        return f"{self.kind} at {self.line}:{self.column}: {self.message}"


def unsupported(feature: str, line: int, column: int) -> PineError:
    """Build the canonical error for a Pine feature this engine does not implement.

    The spec is explicit: unsupported features must fail loudly, never be
    silently approximated.
    """
    return PineError(
        kind=KIND_UNSUPPORTED,
        line=line,
        column=column,
        message=f"Unsupported Pine feature: {feature}",
    )
