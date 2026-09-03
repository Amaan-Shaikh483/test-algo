"""Lexer and parser tests for the Pine subset."""

import pytest

from pine.errors import PineError
from pine.lexer import Lexer
from pine.parser import parse_script


def tokens_of(source: str):
    lexer = Lexer(source)
    return lexer.tokenize()


class TestLexer:
    def test_version_annotation_captured(self):
        lexer = Lexer('//@version=5\nstrategy("X")\n')
        lexer.tokenize()
        assert lexer.version == "5"

    def test_version_annotation_anywhere(self):
        lexer = Lexer('// a comment\n//@version=5\nstrategy("X")\n')
        lexer.tokenize()
        assert lexer.version == "5"

    def test_indent_dedent_tokens(self):
        tokens = tokens_of(
            '//@version=5\nstrategy("X")\nif close > open\n    plot(close)\nplot(open)\n'
        )
        types = [t.type for t in tokens]
        assert "INDENT" in types
        assert "DEDENT" in types

    def test_operators(self):
        tokens = tokens_of("x := 1 + 2 * 3 / 4 % 5\n")
        values = [t.value for t in tokens if t.value]
        for op in (":=", "+", "*", "/", "%"):
            assert op in values

    def test_multi_line_call_no_newline_inside_brackets(self):
        tokens = tokens_of('//@version=5\nstrategy("X",\n  overlay=true)\n')
        # No NEWLINE between the parenthesised lines
        body = [t for t in tokens if t.type == "NEWLINE"]
        assert len(body) <= 1

    def test_string_with_escapes(self):
        tokens = tokens_of('x = "he said \\"hi\\""\n')
        string_token = next(t for t in tokens if t.type == "STRING")
        assert string_token.value == 'he said "hi"'

    def test_numbers(self):
        tokens = tokens_of("x = 3.14\ny = 1e5\nz = 42\n")
        numbers = [t.value for t in tokens if t.type == "NUMBER"]
        assert numbers == ["3.14", "1e5", "42"]

    def test_unterminated_string_raises(self):
        with pytest.raises(PineError) as exc:
            tokens_of('x = "oops\n')
        assert "Unterminated string" in exc.value.message

    def test_line_column_tracked(self):
        tokens = tokens_of('//@version=5\nstrategy("X")\nplot(close)\n')
        plot_token = next(t for t in tokens if t.value == "plot")
        assert plot_token.line == 3
        assert plot_token.column == 1


class TestParser:
    def test_declaration(self):
        script = parse_script('//@version=5\nstrategy("X")\nfast = ta.ema(close, 9)\n')
        declaration = script.statements[1]
        assert declaration.name == "fast"
        assert declaration.value.name == "ta.ema"

    def test_if_else_chain(self):
        script = parse_script(
            '//@version=5\nstrategy("X")\n'
            "if close > open\n"
            "    a = 1\n"
            "else if close < open\n"
            "    a = 2\n"
            "else\n"
            "    a = 3\n"
        )
        statement = script.statements[1]
        assert len(statement.branches) == 2
        assert statement.else_block is not None

    def test_function_definition(self):
        script = parse_script(
            '//@version=5\nstrategy("X")\nmyAvg(src, len) => ta.sma(src, len)\n'
        )
        func = script.statements[1]
        assert func.params == ["src", "len"]

    def test_history_index(self):
        script = parse_script('//@version=5\nstrategy("X")\nx = close[1]\n')
        declaration = script.statements[1]
        assert declaration.value.base.name == "close"
        assert declaration.value.offset.value == 1.0

    def test_named_arguments(self):
        script = parse_script(
            '//@version=5\nstrategy("X")\nplot(close, title="Close", color=color.red)\n'
        )
        call = script.statements[1].expression
        assert "title" in call.kwargs
        assert "color" in call.kwargs

    def test_ternary(self):
        script = parse_script(
            '//@version=5\nstrategy("X")\nx = close > open ? 1 : 0\n'
        )
        assert script.statements[1].value.condition is not None

    def test_var_persistent_declaration(self):
        script = parse_script('//@version=5\nstrategy("X")\nvar float cum = 0.0\n')
        declaration = script.statements[1]
        assert declaration.persistent is True

    def test_reassignment(self):
        script = parse_script(
            '//@version=5\nstrategy("X")\nvar float cum = 0.0\ncum := cum + 1\n'
        )
        assert script.statements[2].name == "cum"

    def test_bad_indentation_raises(self):
        with pytest.raises(PineError):
            parse_script(
                '//@version=5\nstrategy("X")\nif close > open\n  a = 1\n   b = 2\n'
            )

    def test_for_loop_rejected_by_lexer(self):
        with pytest.raises(PineError) as exc:
            parse_script('//@version=5\nstrategy("X")\nfor i = 0 to 10\n    a = 1\n')
        assert "Unsupported Pine feature" in exc.value.message
