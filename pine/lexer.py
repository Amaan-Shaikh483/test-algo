"""Tokenizer for the supported Pine Script subset.

Produces a flat token stream with Python-style INDENT/DEDENT block tokens
(Pine uses indentation for blocks exactly like Python) and NEWLINE separators.
Line/column positions are tracked on every token so later stages can report
diagnostics at the exact source coordinates.

Deliberately simple: no regex-based Pine translation, no string interpolation
support. Only what the documented subset needs.
"""

from dataclasses import dataclass

from pine.errors import KIND_COMPILE, PineError

# Token types
T_NEWLINE = "NEWLINE"
T_INDENT = "INDENT"
T_DEDENT = "DEDENT"
T_ID = "ID"
T_NUMBER = "NUMBER"
T_STRING = "STRING"
T_OP = "OP"
T_VERSION = "VERSION"
T_EOF = "EOF"

KEYWORDS = {
    "if": "if",
    "else": "else",
    "and": "and",
    "or": "or",
    "not": "not",
    "var": "var",
    "true": "true",
    "false": "false",
    "na": "na",
}

# Keywords this engine refuses up front, with the clear error the spec demands.
UNSUPPORTED_KEYWORDS = {
    "varip": "varip",
    "for": "for",
    "while": "while",
    "switch": "switch",
    "import": "import",
    "export": "export",
    "type": "type",
    "method": "method",
    "const": "const",
    "series": "series",
    "simple": "simple",
    "input": "input",  # `input` as a type qualifier, not the input() function
}

OPERATORS = [
    ":=",
    "==",
    "!=",
    "<=",
    ">=",
    "=>",
    "+",
    "-",
    "*",
    "/",
    "%",
    "<",
    ">",
    "=",
    "?",
    ":",
    "(",
    ")",
    "[",
    "]",
    ",",
    ".",
]


@dataclass
class Token:
    """One lexeme with source coordinates."""

    type: str
    value: str
    line: int
    column: int


class Lexer:
    """Turn Pine source text into a token stream."""

    def __init__(self, source: str) -> None:
        # Normalise line endings and expand tabs (Pine editors use spaces).
        self.lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.tokens: list[Token] = []
        self.version: str | None = None
        # Bracket depth > 0 suppresses NEWLINE, allowing multi-line calls.
        self._bracket_depth = 0

    def error(self, line: int, column: int, message: str) -> PineError:
        return PineError(kind=KIND_COMPILE, line=line, column=column, message=message)

    def tokenize(self) -> list[Token]:
        """Lex the whole program. Raises PineError on malformed input."""
        indent_stack = [0]
        line_no = 0
        saw_code = False

        for raw_line in self.lines:
            line_no += 1

            stripped = raw_line.strip()
            is_blank = stripped == ""
            is_comment = stripped.startswith("//")

            # The //@version=5 annotation lives inside a comment. Capture it
            # wherever it appears; the validator enforces placement rules.
            if is_comment and "@version" in stripped:
                self._capture_version(stripped, line_no)

            if is_blank or is_comment:
                continue

            indent = self._indent_width(raw_line, line_no)

            if self._bracket_depth > 0:
                # Continuation line inside an open bracket: no indent tracking.
                self._lex_line(raw_line, line_no)
                continue

            if not saw_code:
                indent_stack = [indent]
                saw_code = True
            elif indent > indent_stack[-1]:
                indent_stack.append(indent)
                self.tokens.append(Token(T_INDENT, "", line_no, indent + 1))
            else:
                while indent < indent_stack[-1]:
                    indent_stack.pop()
                    self.tokens.append(Token(T_DEDENT, "", line_no, indent + 1))
                if indent != indent_stack[-1]:
                    raise self.error(
                        line_no,
                        indent + 1,
                        "Inconsistent indentation: expected an indentation level of "
                        f"{indent_stack[-1]} spaces, found {indent}",
                    )

            self._lex_line(raw_line, line_no)

            # A statement line always ends with NEWLINE (unless the bracket
            # depth swallowed it, which _lex_line tracks).
            if self._bracket_depth == 0:
                self.tokens.append(Token(T_NEWLINE, "", line_no, len(raw_line) + 1))

        line_no = len(self.lines)
        while indent_stack and indent_stack[-1] > 0:
            indent_stack.pop()
            self.tokens.append(Token(T_DEDENT, "", line_no, 1))
        self.tokens.append(Token(T_EOF, "", line_no + 1, 1))
        return self.tokens

    def _capture_version(self, comment: str, line_no: int) -> None:
        body = comment.lstrip("/").strip()
        if not body.startswith("@version"):
            return
        _, _, value = body.partition("=")
        value = value.strip()
        if not value:
            raise self.error(line_no, 1, "//@version annotation is missing a value")
        self.version = value

    def _indent_width(self, raw_line: str, line_no: int) -> int:
        width = 0
        for ch in raw_line:
            if ch == " ":
                width += 1
            elif ch == "\t":
                width += 4
            else:
                break
        return width

    def _lex_line(self, raw_line: str, line_no: int) -> None:
        """Lex one physical line, honouring in-line comments and strings."""
        i = self._indent_width(raw_line, line_no)
        length = len(raw_line)

        while i < length:
            ch = raw_line[i]

            if ch in (" ", "\t"):
                i += 1
                continue

            # Comment runs to end of line.
            if ch == "/" and i + 1 < length and raw_line[i + 1] == "/":
                return

            column = i + 1

            if ch == '"':
                token, i = self._lex_string(raw_line, i, line_no)
                self.tokens.append(token)
                continue

            if ch.isdigit() or (ch == "." and i + 1 < length and raw_line[i + 1].isdigit()):
                token, i = self._lex_number(raw_line, i, line_no)
                self.tokens.append(token)
                continue

            if ch.isalpha() or ch == "_":
                j = i
                while j < length and (raw_line[j].isalnum() or raw_line[j] == "_"):
                    j += 1
                word = raw_line[i:j]
                i = j

                if word in UNSUPPORTED_KEYWORDS and not self._is_qualified_name(word):
                    raise PineError(
                        kind="unsupported_feature",
                        line=line_no,
                        column=column,
                        message=f"Unsupported Pine feature: '{word}' keyword",
                    )
                if word in KEYWORDS:
                    self.tokens.append(Token(KEYWORDS[word], word, line_no, column))
                else:
                    self.tokens.append(Token(T_ID, word, line_no, column))
                continue

            matched = False
            for op in OPERATORS:
                if raw_line.startswith(op, i):
                    if op in ("(", "["):
                        self._bracket_depth += 1
                    elif op in (")", "]"):
                        self._bracket_depth = max(0, self._bracket_depth - 1)
                    self.tokens.append(Token(T_OP, op, line_no, column))
                    i += len(op)
                    matched = True
                    break
            if not matched:
                raise self.error(line_no, column, f"Unexpected character '{ch}'")

    def _is_qualified_name(self, word: str) -> bool:
        """True when a keyword like ``type`` appears as a Pine namespace member.

        ``ta.type`` or ``strategy.type`` is a plain dotted name, not the
        unsupported keyword. Look at the previous non-dot token.
        """
        for token in reversed(self.tokens):
            if token.type == T_OP and token.value == ".":
                return True
            if token.type == T_ID:
                # e.g. `ta` immediately before this word -> qualified member
                return True
            return False
        return False

    def _lex_string(self, line: str, start: int, line_no: int) -> tuple[Token, int]:
        """Lex a double-quoted string literal with simple escapes."""
        out = []
        i = start + 1
        length = len(line)
        while i < length:
            ch = line[i]
            if ch == "\\" and i + 1 < length:
                nxt = line[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if ch == '"':
                return Token(T_STRING, "".join(out), line_no, start + 1), i + 1
            out.append(ch)
            i += 1
        raise self.error(line_no, start + 1, "Unterminated string literal")

    def _lex_number(self, line: str, start: int, line_no: int) -> tuple[Token, int]:
        i = start
        length = len(line)
        seen_dot = False
        seen_exp = False
        while i < length:
            ch = line[i]
            if ch.isdigit():
                i += 1
            elif ch == "." and not seen_dot and not seen_exp:
                # Only a decimal point when a digit follows (else it's a
                # member access like `ta.ema`).
                if i + 1 < length and line[i + 1].isdigit():
                    seen_dot = True
                    i += 1
                else:
                    break
            elif ch in ("e", "E") and not seen_exp and i + 1 < length:
                nxt = line[i + 1]
                if nxt.isdigit() or (nxt in "+-" and i + 2 < length and line[i + 2].isdigit()):
                    seen_exp = True
                    i += 2 if nxt in "+-" else 1
                else:
                    break
            else:
                break
        return Token(T_NUMBER, line[start:i], line_no, start + 1), i


def tokenize(source: str) -> tuple[list[Token], str | None]:
    """Convenience wrapper: returns (tokens, version annotation)."""
    lexer = Lexer(source)
    tokens = lexer.tokenize()
    return tokens, lexer.version
