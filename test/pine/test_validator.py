"""Semantic validator tests: structure, names, unsupported features."""

import pytest

from pine.compiler import compile_script


def compile_error(source: str):
    result = compile_script(source)
    assert not result.ok, "expected a compile failure"
    assert result.error is not None
    return result.error


class TestScriptStructure:
    def test_version_required(self):
        error = compile_error('strategy("X")\nplot(close)\n')
        assert "version" in error.message.lower()

    def test_only_version_5(self):
        error = compile_error('//@version=4\nstrategy("X")\n')
        assert error.kind == "unsupported_feature"
        assert "version" in error.message

    def test_header_required(self):
        error = compile_error('//@version=5\nplot(close)\n')
        assert "indicator" in error.message and "strategy" in error.message

    def test_header_needs_string_title(self):
        error = compile_error('//@version=5\nstrategy(42)\n')
        assert "title" in error.message

    def test_duplicate_header_rejected(self):
        error = compile_error('//@version=5\nindicator("A")\nindicator("B")\n')
        assert "Duplicate" in error.message

    def test_empty_script(self):
        error = compile_error("//@version=5\n")
        assert "empty" in error.message.lower()


class TestUnsupportedFeatures:
    def test_request_security(self):
        source = (
            '//@version=5\nstrategy("X", overlay=true)\n'
            'a = request.security("NSE:NIFTY", "1", close)\n'
        )
        error = compile_error(source)
        assert error.kind == "unsupported_feature"
        assert error.message == "Unsupported Pine feature: request.security()"
        assert error.line == 3
        assert error.column == 5

    def test_arrays(self):
        source = '//@version=5\nstrategy("X")\na = array.new_float(10)\n'
        error = compile_error(source)
        assert "Unsupported Pine feature: array.new_float()" in error.message

    def test_for_loop(self):
        source = '//@version=5\nstrategy("X")\nfor i = 0 to 10\n    plot(close)\n'
        error = compile_error(source)
        assert "for" in error.message

    def test_switch(self):
        source = '//@version=5\nstrategy("X")\nswitch close\n    => 1\n'
        error = compile_error(source)
        assert "switch" in error.message

    def test_error_shape_matches_spec(self):
        source = (
            '//@version=5\nstrategy("X", overlay=true)\n'
            'value = request.security("NSE:NIFTY", "1", close)\n'
        )
        result = compile_script(source)
        payload = result.error.to_dict()
        assert payload["type"] == "compile_error"
        assert payload["line"] == 3
        assert payload["column"] == 9
        assert payload["message"] == "Unsupported Pine feature: request.security()"


class TestReferences:
    def test_undeclared_identifier(self):
        error = compile_error('//@version=5\nstrategy("X")\nplot(unknownName)\n')
        assert "unknownName" in error.message

    def test_unknown_function(self):
        error = compile_error('//@version=5\nstrategy("X")\nfoo.bar(close)\n')
        assert "Unknown" in error.message

    def test_unknown_ta_function_is_unsupported(self):
        error = compile_error('//@version=5\nstrategy("X")\nx = ta.macd(close, 12, 26, 9)\n')
        assert error.kind == "unsupported_feature"

    def test_wrong_argument_count(self):
        error = compile_error('//@version=5\nstrategy("X")\nx = ta.ema(close)\n')
        assert "expects 2" in error.message

    def test_statement_call_in_expression(self):
        error = compile_error('//@version=5\nstrategy("X")\nx = plot(close)\n')
        assert "standalone statement" in error.message

    def test_namespaced_constants_accepted(self):
        result = compile_script(
            '//@version=5\nindicator("A")\n'
            'plotshape(close > open, style=shape.triangleup, location=location.belowbar, '
            'color=color.green)\n'
        )
        assert result.ok

    def test_builtin_series_accepted(self):
        result = compile_script(
            '//@version=5\nindicator("A")\n'
            'plot((open + high + low + close) / 4)\n'
        )
        assert result.ok
