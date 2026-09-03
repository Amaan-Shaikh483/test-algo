"""Recursive-descent parser for the Pine subset.

Consumes the lexer's token stream and builds the AST defined in
``pine/ast_nodes.py``. The grammar is intentionally small: declarations,
reassignments, single-expression user functions, if/else blocks, calls with
positional and named arguments, ternaries, boolean/arithmetic operators and
history indexing (``close[1]``).
"""

from pine import ast_nodes as ast
from pine.errors import KIND_COMPILE, PineError
from pine.lexer import (
    T_DEDENT,
    T_EOF,
    T_ID,
    T_INDENT,
    T_NEWLINE,
    T_NUMBER,
    T_OP,
    T_STRING,
    Token,
    tokenize,
)

BINARY_OPS = {"+", "-", "*", "/", "%", "==", "!=", "<", ">", "<=", ">=", "and", "or"}


class Parser:
    """Build an AST from Pine source text."""

    def __init__(self, source: str) -> None:
        self.tokens, version = tokenize(source)
        self.version = version
        self.pos = 0

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        token = self.current
        if token.type != T_EOF:
            self.pos += 1
        return token

    def check(self, token_type: str, value: str | None = None) -> bool:
        token = self.current
        return token.type == token_type and (value is None or token.value == value)

    def match(self, token_type: str, value: str | None = None) -> Token | None:
        if self.check(token_type, value):
            return self.advance()
        return None

    def expect(self, token_type: str, value: str | None = None, what: str = "") -> Token:
        if self.check(token_type, value):
            return self.advance()
        token = self.current
        expected = what or value or token_type
        found = token.value if token.value else token.type.lower()
        raise PineError(
            kind=KIND_COMPILE,
            line=token.line,
            column=token.column,
            message=f"Expected {expected} but found '{found}'",
        )

    def skip_newlines(self) -> None:
        while self.check(T_NEWLINE):
            self.advance()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def parse(self) -> ast.Script:
        script = ast.Script(line=1, column=1, version=self.version)
        self.skip_newlines()
        while not self.check(T_EOF):
            if self.match(T_INDENT):
                raise PineError(
                    kind=KIND_COMPILE,
                    line=self.current.line,
                    column=self.current.column,
                    message="Unexpected indentation at top level",
                )
            if self.match(T_DEDENT):
                continue
            statement = self.parse_statement()
            if statement is not None:
                script.statements.append(statement)
            self.skip_newlines()
        return script

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def parse_statement(self) -> ast.Node | None:
        token = self.current

        if token.type == "if":
            return self.parse_if_statement()

        if token.type == "var":
            return self.parse_declaration()

        if token.type == T_ID:
            # name := expr  (reassignment)
            if self.peek().type == T_OP and self.peek().value == ":=":
                return self.parse_reassignment()
            # name = expr / name : type = expr  (declaration)
            if self.peek().type == T_OP and self.peek().value == "=":
                return self.parse_declaration()
            # typed declaration: float name = expr / int name := expr
            if (
                self.peek().type == T_ID
                and self.peek(2).type == T_OP
                and self.peek(2).value in ("=", ":=")
            ):
                return self.parse_declaration()
            # name(params) => expr  (function definition)
            if self.peek().type == T_OP and self.peek().value == "(":
                if self._is_function_def():
                    return self.parse_function_def()
            # qualified statement call, e.g. plot(...) / ta.ema(...)
            if self.peek().type == T_OP and self.peek().value == ".":
                expr = self.parse_expression()
                return ast.ExpressionStatement(
                    line=expr.line, column=expr.column, expression=expr
                )

        expr = self.parse_expression()
        return ast.ExpressionStatement(line=expr.line, column=expr.column, expression=expr)

    def _is_function_def(self) -> bool:
        """Look ahead past the parameter list for the ``=>`` arrow."""
        depth = 0
        idx = self.pos + 1  # at '('
        while idx < len(self.tokens):
            token = self.tokens[idx]
            if token.type == T_OP and token.value in ("(", "["):
                depth += 1
            elif token.type == T_OP and token.value in (")", "]"):
                depth -= 1
                if depth == 0:
                    nxt = idx + 1
                    while nxt < len(self.tokens) and self.tokens[nxt].type == T_NEWLINE:
                        nxt += 1
                    return (
                        nxt < len(self.tokens)
                        and self.tokens[nxt].type == T_OP
                        and self.tokens[nxt].value == "=>"
                    )
            elif token.type == T_NEWLINE and depth == 0:
                return False
            idx += 1
        return False

    def parse_declaration(self) -> ast.Declaration:
        start = self.current
        persistent = bool(self.match("var"))
        # Optional explicit type: `var float x = ...` or `float x = ...`.
        # The type is accepted and ignored (values are dynamically typed).
        declared_type = None
        if (
            self.current.type == T_ID
            and self.peek().type == T_ID
            and self.peek().value not in ("and", "or", "not", "if", "else")
        ):
            declared_type = self.advance().value
        # Optional `name : type` form.
        if self.match(T_OP, ":"):
            type_token = self.expect(T_ID, what="type name")
            declared_type = type_token.value
        name_token = self.expect(T_ID, what="variable name")
        if self.match(T_OP, ":="):
            self.skip_newlines()
            value = self.parse_expression()
            return ast.Reassignment(
                line=start.line,
                column=start.column,
                name=name_token.value,
                value=value,
            )
        self.expect(T_OP, "=")
        self.skip_newlines()
        value = self.parse_expression()
        return ast.Declaration(
            line=start.line,
            column=start.column,
            name=name_token.value,
            value=value,
            persistent=persistent,
            declared_type=declared_type,
        )

    def parse_reassignment(self) -> ast.Reassignment:
        start = self.current
        name_token = self.expect(T_ID, what="variable name")
        self.expect(T_OP, ":=")
        self.skip_newlines()
        value = self.parse_expression()
        return ast.Reassignment(
            line=start.line,
            column=start.column,
            name=name_token.value,
            value=value,
        )

    def parse_function_def(self) -> ast.FunctionDef:
        start = self.current
        name_token = self.expect(T_ID, what="function name")
        self.expect(T_OP, "(")
        params: list[str] = []
        while not self.check(T_OP, ")"):
            param = self.expect(T_ID, what="parameter name")
            params.append(param.value)
            # Optional typed parameter `x : float` - type is accepted and ignored.
            if self.check(T_OP, ":"):
                self.advance()
                self.expect(T_ID, what="parameter type")
            if not self.match(T_OP, ","):
                break
        self.expect(T_OP, ")")
        self.expect(T_OP, "=>")
        body = self.parse_expression()
        return ast.FunctionDef(
            line=start.line,
            column=start.column,
            name=name_token.value,
            params=params,
            body=body,
        )

    def parse_if_statement(self) -> ast.IfStatement:
        start = self.expect("if")
        condition = self.parse_expression()
        if_statement = ast.IfStatement(line=start.line, column=start.column)

        branch = ast.IfBranch(line=start.line, column=start.column, condition=condition)
        branch.block = self.parse_block()
        if_statement.branches.append(branch)

        while True:
            self.skip_newlines()
            if self.check("else"):
                else_token = self.advance()
                # `else if` chains: another condition, another branch.
                if self.check("if"):
                    self.advance()
                    next_condition = self.parse_expression()
                    next_branch = ast.IfBranch(
                        line=else_token.line, column=else_token.column, condition=next_condition
                    )
                    next_branch.block = self.parse_block()
                    if_statement.branches.append(next_branch)
                    continue
                if_statement.else_block = self.parse_block()
                break
            break

        return if_statement

    def parse_block(self) -> list:
        """An indented statement block: ``NEWLINE INDENT stmt+ DEDENT``."""
        self.skip_newlines()
        self.expect(T_INDENT, what="an indented block")
        statements: list = []
        self.skip_newlines()
        while not self.check(T_DEDENT) and not self.check(T_EOF):
            statement = self.parse_statement()
            if statement is not None:
                statements.append(statement)
            self.skip_newlines()
        self.match(T_DEDENT)
        if not statements:
            token = self.current
            raise PineError(
                kind=KIND_COMPILE,
                line=token.line,
                column=token.column,
                message="Empty block: expected at least one statement",
            )
        return statements

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def parse_expression(self) -> ast.Expr:
        return self.parse_ternary()

    def parse_ternary(self) -> ast.Expr:
        condition = self.parse_or()
        if self.match(T_OP, "?"):
            self.skip_newlines()
            then_expr = self.parse_expression()
            self.skip_newlines()
            self.expect(T_OP, ":")
            self.skip_newlines()
            else_expr = self.parse_expression()
            return ast.TernaryOp(
                line=condition.line,
                column=condition.column,
                condition=condition,
                then_expr=then_expr,
                else_expr=else_expr,
            )
        return condition

    def parse_or(self) -> ast.Expr:
        left = self.parse_and()
        while self.check("or"):
            op_token = self.advance()
            self.skip_newlines()
            right = self.parse_and()
            left = ast.BinaryOp(
                line=op_token.line, column=op_token.column, op="or", left=left, right=right
            )
        return left

    def parse_and(self) -> ast.Expr:
        left = self.parse_not()
        while self.check("and"):
            op_token = self.advance()
            self.skip_newlines()
            right = self.parse_not()
            left = ast.BinaryOp(
                line=op_token.line, column=op_token.column, op="and", left=left, right=right
            )
        return left

    def parse_not(self) -> ast.Expr:
        if self.check("not"):
            token = self.advance()
            operand = self.parse_not()
            return ast.UnaryOp(line=token.line, column=token.column, op="not", operand=operand)
        return self.parse_comparison()

    def parse_comparison(self) -> ast.Expr:
        left = self.parse_additive()
        while self.current.type == T_OP and self.current.value in (
            "==",
            "!=",
            "<",
            ">",
            "<=",
            ">=",
        ):
            op_token = self.advance()
            self.skip_newlines()
            right = self.parse_additive()
            left = ast.BinaryOp(
                line=op_token.line,
                column=op_token.column,
                op=op_token.value,
                left=left,
                right=right,
            )
        return left

    def parse_additive(self) -> ast.Expr:
        left = self.parse_multiplicative()
        while self.current.type == T_OP and self.current.value in ("+", "-"):
            op_token = self.advance()
            self.skip_newlines()
            right = self.parse_multiplicative()
            left = ast.BinaryOp(
                line=op_token.line,
                column=op_token.column,
                op=op_token.value,
                left=left,
                right=right,
            )
        return left

    def parse_multiplicative(self) -> ast.Expr:
        left = self.parse_unary()
        while self.current.type == T_OP and self.current.value in ("*", "/", "%"):
            op_token = self.advance()
            self.skip_newlines()
            right = self.parse_unary()
            left = ast.BinaryOp(
                line=op_token.line,
                column=op_token.column,
                op=op_token.value,
                left=left,
                right=right,
            )
        return left

    def parse_unary(self) -> ast.Expr:
        if self.current.type == T_OP and self.current.value in ("-", "+"):
            token = self.advance()
            operand = self.parse_unary()
            if token.value == "+":
                return operand
            return ast.UnaryOp(line=token.line, column=token.column, op="-", operand=operand)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        expr = self.parse_primary()
        while self.check(T_OP, "["):
            bracket = self.advance()
            self.skip_newlines()
            offset = self.parse_expression()
            self.skip_newlines()
            self.expect(T_OP, "]")
            expr = ast.HistoryRef(
                line=bracket.line, column=bracket.column, base=expr, offset=offset
            )
        return expr

    def parse_primary(self) -> ast.Expr:
        token = self.current

        if token.type == T_NUMBER:
            self.advance()
            value = float(token.value)
            return ast.NumberLiteral(line=token.line, column=token.column, value=value)

        if token.type == T_STRING:
            self.advance()
            return ast.StringLiteral(line=token.line, column=token.column, value=token.value)

        if token.type == "true":
            self.advance()
            return ast.BoolLiteral(line=token.line, column=token.column, value=True)

        if token.type == "false":
            self.advance()
            return ast.BoolLiteral(line=token.line, column=token.column, value=False)

        if token.type == "na":
            # `na` is the literal, but `na(x)` is the is-na test function.
            if self.peek().type == T_OP and self.peek().value == "(":
                self.advance()  # consume the na keyword itself
                return self.parse_call("na", token)
            self.advance()
            return ast.NaLiteral(line=token.line, column=token.column)

        if token.type == "if":
            # if-expression (e.g. plot(if cond then-value else-value)) is not
            # supported as an expression; fail clearly rather than mis-parse.
            raise PineError(
                kind="unsupported_feature",
                line=token.line,
                column=token.column,
                message="Unsupported Pine feature: 'if' used as an expression",
            )

        if token.type == T_OP and token.value == "(":
            self.advance()
            self.skip_newlines()
            expr = self.parse_expression()
            self.skip_newlines()
            self.expect(T_OP, ")")
            return expr

        if token.type == T_ID:
            # Dotted name: ta.ema, strategy.entry, strategy.long ...
            parts = [self.advance().value]
            while self.check(T_OP, "."):
                dot = self.advance()
                member = self.expect(T_ID, what="name after '.'")
                parts.append(member.value)
                _ = dot
            name = ".".join(parts)

            if self.check(T_OP, "("):
                return self.parse_call(name, token)

            return ast.VarRef(line=token.line, column=token.column, name=name)

        found = token.value if token.value else token.type.lower()
        raise PineError(
            kind=KIND_COMPILE,
            line=token.line,
            column=token.column,
            message=f"Unexpected token '{found}' in expression",
        )

    def parse_call(self, name: str, start_token: Token) -> ast.Call:
        self.expect(T_OP, "(")
        args: list[ast.Expr] = []
        kwargs: dict[str, ast.Expr] = {}
        self.skip_newlines()

        while not self.check(T_OP, ")"):
            # Named argument: identifier '=' (but not '==')
            if (
                self.current.type == T_ID
                and self.peek().type == T_OP
                and self.peek().value == "="
            ):
                key_token = self.advance()
                self.advance()  # '='
                self.skip_newlines()
                kwargs[key_token.value] = self.parse_expression()
            else:
                args.append(self.parse_expression())
            self.skip_newlines()
            if not self.match(T_OP, ","):
                break
            self.skip_newlines()

        self.expect(T_OP, ")")
        return ast.Call(
            line=start_token.line, column=start_token.column, name=name, args=args, kwargs=kwargs
        )


def parse_script(source: str) -> ast.Script:
    """Parse Pine source into a Script AST."""
    return Parser(source).parse()
