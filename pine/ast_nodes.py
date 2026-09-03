"""AST node definitions for the Pine subset.

Every node carries the line/column of its first token so runtime and
validation errors point at real source coordinates. Nodes are plain
dataclasses: the interpreter walks them directly and there is no code
generation of any kind (no eval/exec anywhere in the engine).
"""

from dataclasses import dataclass, field


@dataclass
class Node:
    """Base class carrying source coordinates."""

    line: int = 0
    column: int = 0


# --------------------------------------------------------------------------
# Script
# --------------------------------------------------------------------------


@dataclass
class Script(Node):
    """Whole program: version annotation plus top-level statements."""

    version: str | None = None
    statements: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


@dataclass
class Declaration(Node):
    """``name = expr`` or ``var name = expr`` (persistent) or typed ``name : float = expr``."""

    name: str = ""
    value: "Expr | None" = None
    persistent: bool = False  # declared with `var`
    declared_type: str | None = None


@dataclass
class Reassignment(Node):
    """``name := expr`` (mutates an existing variable on the current bar)."""

    name: str = ""
    value: "Expr | None" = None


@dataclass
class FunctionDef(Node):
    """``name(params) => expr`` single-expression user function."""

    name: str = ""
    params: list = field(default_factory=list)  # parameter names
    body: "Expr | None" = None


@dataclass
class IfBranch(Node):
    """One ``if``/``else if`` arm: condition plus its block."""

    condition: "Expr | None" = None
    block: list = field(default_factory=list)  # statements


@dataclass
class IfStatement(Node):
    """``if`` statement with ordered branches and optional else block."""

    branches: list = field(default_factory=list)  # IfBranch
    else_block: list | None = None


@dataclass
class ExpressionStatement(Node):
    """A bare expression (plot(...), strategy.entry(...), alert(...))."""

    expression: "Expr | None" = None


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


@dataclass
class Expr(Node):
    """Base expression node."""

    pass


@dataclass
class NumberLiteral(Expr):
    value: float = 0.0


@dataclass
class StringLiteral(Expr):
    value: str = ""


@dataclass
class BoolLiteral(Expr):
    value: bool = False


@dataclass
class NaLiteral(Expr):
    """The ``na`` literal."""

    pass


@dataclass
class VarRef(Expr):
    """Reference to a variable or namespaced constant (strategy.long)."""

    name: str = ""


@dataclass
class BinaryOp(Expr):
    op: str = ""
    left: Expr | None = None
    right: Expr | None = None


@dataclass
class UnaryOp(Expr):
    op: str = ""
    operand: Expr | None = None


@dataclass
class TernaryOp(Expr):
    condition: Expr | None = None
    then_expr: Expr | None = None
    else_expr: Expr | None = None


@dataclass
class Call(Expr):
    """Function call; the callee is a dotted name such as ``ta.ema``."""

    name: str = ""
    args: list = field(default_factory=list)  # positional Expr
    kwargs: dict = field(default_factory=dict)  # name -> Expr


@dataclass
class HistoryRef(Expr):
    """``expr[offset]`` historical value access."""

    base: Expr | None = None
    offset: Expr | None = None


ExprType = Expr
