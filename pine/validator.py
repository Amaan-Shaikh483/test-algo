"""Semantic validation for the Pine subset.

Runs after parsing, before any execution. Responsibilities:

* version annotation present and equal to 5
* exactly one ``indicator()``/``strategy()`` declaration, and it is first
* every referenced name is either a built-in, a built-in series (close, ...),
  a user declaration, a user function or a namespaced constant
* call-argument counts match the built-in signatures
* known-but-unimplemented Pine features fail with the explicit
  ``Unsupported Pine feature: ...`` error required by the spec
* statement-role built-ins (plot, strategy.entry, ...) may not appear inside
  expressions

The validator never looks at values; it is purely structural.
"""

from pine import ast_nodes as ast
from pine.builtins import (
    KNOWN_NAMESPACES,
    SUPPORTED_BUILTINS,
    SUPPORTED_CONSTANT_PREFIXES,
    SUPPORTED_CONSTANTS,
    UNSUPPORTED_FEATURES,
    builtin_spec,
)
from pine.errors import KIND_COMPILE, PineError

# Built-in series available on every bar.
BUILTIN_SERIES = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "time",
    "bar_index",
    "hl2",
    "hlc3",
    "ohlc4",
    "na",
}

STATEMENT_ROLE_NAMES = {
    name for name, spec in SUPPORTED_BUILTINS.items() if spec.role == "statement"
}

# Declaration calls that may appear as the script header.
HEADER_CALLS = {"indicator", "strategy"}


class Validator:
    """Walks a parsed Script and raises PineError on any violation."""

    def __init__(self, script: ast.Script) -> None:
        self.script = script
        self.errors: list[PineError] = []

    def validate(self) -> None:
        """Validate the script. Raises the first error found."""
        self._validate_version()
        self._validate_header()

        declared: set[str] = set()
        self._collect_declarations(self.script.statements, declared)

        for statement in self.script.statements:
            self._walk_statement(statement, declared, in_scope=set())

    # ------------------------------------------------------------------
    # Structure checks
    # ------------------------------------------------------------------

    def _validate_version(self) -> None:
        version = self.script.version
        if version is None:
            raise PineError(
                kind=KIND_COMPILE,
                line=1,
                column=1,
                message="Missing //@version=5 annotation on the first line",
            )
        if version != "5":
            raise PineError(
                kind="unsupported_feature",
                line=1,
                column=1,
                message=(
                    f"Unsupported Pine version: {version}. Only //@version=5 is supported"
                ),
            )

    def _validate_header(self) -> None:
        if not self.script.statements:
            raise PineError(
                kind=KIND_COMPILE, line=1, column=1, message="Script is empty"
            )
        first = self.script.statements[0]
        header_name = None
        if isinstance(first, ast.ExpressionStatement) and isinstance(first.expression, ast.Call):
            header_name = first.expression.name
        if header_name not in HEADER_CALLS:
            raise PineError(
                kind=KIND_COMPILE,
                line=first.line,
                column=first.column,
                message=(
                    "The first statement must be indicator(...) or strategy(...) "
                    "with a quoted title"
                ),
            )
        call = first.expression
        if not call.args or not isinstance(call.args[0], ast.StringLiteral):
            raise PineError(
                kind=KIND_COMPILE,
                line=call.line,
                column=call.column,
                message=f"{header_name}() requires a string title as its first argument",
            )
        # Exactly one header call for the whole script.
        for statement in self.script.statements[1:]:
            if (
                isinstance(statement, ast.ExpressionStatement)
                and isinstance(statement.expression, ast.Call)
                and statement.expression.name in HEADER_CALLS
            ):
                raise PineError(
                    kind=KIND_COMPILE,
                    line=statement.line,
                    column=statement.column,
                    message=(
                        f"Duplicate {statement.expression.name}() declaration: "
                        "a script declares indicator() or strategy() exactly once"
                    ),
                )

    # ------------------------------------------------------------------
    # Name collection
    # ------------------------------------------------------------------

    def _collect_declarations(self, statements: list, declared: set[str]) -> None:
        """Collect every declared name (global and block-local) for reference checks.

        Pine scoping means a block-local name is only visible inside its block;
        for validation purposes treating all declarations as 'known names' is
        sufficient - misuse across sibling blocks is caught at runtime.
        """
        for statement in statements:
            if isinstance(statement, ast.Declaration):
                declared.add(statement.name)
            elif isinstance(statement, ast.Reassignment):
                declared.add(statement.name)  # reassignments imply a prior declaration
            elif isinstance(statement, ast.FunctionDef):
                declared.add(statement.name)
                for param in statement.params:
                    declared.add(param)
            elif isinstance(statement, ast.IfStatement):
                for branch in statement.branches:
                    self._collect_declarations(branch.block, declared)
                if statement.else_block is not None:
                    self._collect_declarations(statement.else_block, declared)

    # ------------------------------------------------------------------
    # Statement walking
    # ------------------------------------------------------------------

    def _walk_statement(self, statement: ast.Node, declared: set[str], in_scope: set[str]) -> None:
        if isinstance(statement, ast.Declaration):
            self._walk_expr(statement.value, declared, in_scope, allow_statement_calls=False)
        elif isinstance(statement, ast.Reassignment):
            self._walk_expr(statement.value, declared, in_scope, allow_statement_calls=False)
        elif isinstance(statement, ast.FunctionDef):
            local = set(statement.params)
            self._walk_expr(statement.body, declared, local, allow_statement_calls=False)
        elif isinstance(statement, ast.IfStatement):
            for branch in statement.branches:
                self._walk_expr(branch.condition, declared, in_scope, allow_statement_calls=False)
                block_scope = set(in_scope)
                for inner in branch.block:
                    self._walk_statement(inner, declared, block_scope)
            if statement.else_block is not None:
                block_scope = set(in_scope)
                for inner in statement.else_block:
                    self._walk_statement(inner, declared, block_scope)
        elif isinstance(statement, ast.ExpressionStatement):
            if not isinstance(statement.expression, ast.Call):
                raise PineError(
                    kind=KIND_COMPILE,
                    line=statement.line,
                    column=statement.column,
                    message="Expression statements must be function calls",
                )
            self._walk_expr(statement.expression, declared, in_scope, allow_statement_calls=True)

    # ------------------------------------------------------------------
    # Expression walking
    # ------------------------------------------------------------------

    def _walk_expr(
        self,
        expr: ast.Expr | None,
        declared: set[str],
        in_scope: set[str],
        allow_statement_calls: bool,
    ) -> None:
        if expr is None:
            return

        if isinstance(expr, ast.NumberLiteral | ast.StringLiteral | ast.BoolLiteral | ast.NaLiteral):
            return

        if isinstance(expr, ast.VarRef):
            self._check_reference(expr, declared, in_scope)
            return

        if isinstance(expr, ast.Call):
            self._check_call(expr, declared, in_scope, allow_statement_calls)
            return

        if isinstance(expr, ast.BinaryOp):
            self._walk_expr(expr.left, declared, in_scope, allow_statement_calls)
            self._walk_expr(expr.right, declared, in_scope, allow_statement_calls)
            return

        if isinstance(expr, ast.UnaryOp):
            self._walk_expr(expr.operand, declared, in_scope, allow_statement_calls)
            return

        if isinstance(expr, ast.TernaryOp):
            self._walk_expr(expr.condition, declared, in_scope, allow_statement_calls)
            self._walk_expr(expr.then_expr, declared, in_scope, allow_statement_calls)
            self._walk_expr(expr.else_expr, declared, in_scope, allow_statement_calls)
            return

        if isinstance(expr, ast.HistoryRef):
            self._walk_expr(expr.base, declared, in_scope, allow_statement_calls)
            self._walk_expr(expr.offset, declared, in_scope, allow_statement_calls)
            return

    def _check_reference(self, ref: ast.VarRef, declared: set[str], in_scope: set[str]) -> None:
        name = ref.name
        if name in BUILTIN_SERIES or name in declared or name in in_scope:
            return
        if name in UNSUPPORTED_FEATURES:
            raise _unsupported_at(ref, name)
        if name in SUPPORTED_CONSTANTS or name.startswith(SUPPORTED_CONSTANT_PREFIXES):
            return
        if "." in name:
            namespace = name.split(".")[0]
            if namespace in KNOWN_NAMESPACES or namespace in BUILTIN_SERIES:
                raise PineError(
                    kind=KIND_COMPILE,
                    line=ref.line,
                    column=ref.column,
                    message=f"Unknown Pine name: {name}",
                )
        raise PineError(
            kind=KIND_COMPILE,
            line=ref.line,
            column=ref.column,
            message=f"Undeclared identifier: '{name}'",
        )

    def _check_call(
        self, call: ast.Call, declared: set[str], in_scope: set[str], allow_statement_calls: bool
    ) -> None:
        name = call.name

        if name in UNSUPPORTED_FEATURES:
            raise _unsupported_at(call, name)

        spec = builtin_spec(name)

        # na(x) is a real function even though `na` is also the series literal.
        if spec is None and name in BUILTIN_SERIES:
            raise PineError(
                kind=KIND_COMPILE,
                line=call.line,
                column=call.column,
                message=f"'{name}' is a built-in series, not a function",
            )

        if spec is None:
            if name in declared:
                return  # user-defined function
            namespace = name.split(".")[0] if "." in name else None
            if namespace and (namespace in KNOWN_NAMESPACES or namespace == "ta"):
                raise PineError(
                    kind="unsupported_feature",
                    line=call.line,
                    column=call.column,
                    message=(
                        f"Unsupported Pine feature: {name}() is not implemented in this engine"
                    ),
                )
            raise PineError(
                kind=KIND_COMPILE,
                line=call.line,
                column=call.column,
                message=f"Unknown function: {name}()",
            )

        if spec.role == "statement" and not allow_statement_calls:
            raise PineError(
                kind=KIND_COMPILE,
                line=call.line,
                column=call.column,
                message=f"{name}() can only be used as a standalone statement",
            )

        total_args = len(call.args) + len(call.kwargs)
        if total_args < spec.min_args or (spec.max_args >= 0 and total_args > spec.max_args):
            expected = (
                f"at least {spec.min_args}"
                if spec.max_args < 0
                else (
                    f"{spec.min_args}"
                    if spec.min_args == spec.max_args
                    else f"{spec.min_args} to {spec.max_args}"
                )
            )
            raise PineError(
                kind=KIND_COMPILE,
                line=call.line,
                column=call.column,
                message=f"{name}() expects {expected} argument(s), got {total_args}",
            )

        for arg in call.args:
            self._walk_expr(arg, declared, in_scope, allow_statement_calls=False)
        for value in call.kwargs.values():
            self._walk_expr(value, declared, in_scope, allow_statement_calls=False)


def _unsupported_at(node: ast.Node, name: str) -> PineError:
    return PineError(
        kind="unsupported_feature",
        line=node.line,
        column=node.column,
        message=f"Unsupported Pine feature: {name}()",
    )


def validate_script(script: ast.Script) -> None:
    """Validate a parsed script; raises PineError on the first violation."""
    Validator(script).validate()
