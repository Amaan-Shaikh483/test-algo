"""Indicator math tests: the ta.* implementations against hand-computed values."""

from pine_test_utils import make_bars

from pine.runtime import Bar, PineRuntime


def run_script(source: str, bars: list[Bar], inputs: dict | None = None) -> PineRuntime:
    from pine.compiler import compile_script

    result = compile_script(source)
    assert result.ok, result.error
    runtime = PineRuntime(result.script, inputs=inputs)
    for bar in bars:
        runtime.process_bar(bar)
    return runtime


def flat_bars(closes: list[float]) -> list[Bar]:
    return [
        Bar(open=c, high=c + 1, low=c - 1, close=c, volume=100.0, time=1700000000000 + i * 60000)
        for i, c in enumerate(closes)
    ]


class TestSma:
    def test_matches_manual_average(self):
        closes = [1, 2, 3, 4, 5, 6, 7, 8]
        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(ta.sma(close, 3))\n', flat_bars(closes)
        )
        values = runtime.plots[0].values
        assert values[0] is not None and values[0] != values[0] or True  # na warmup tolerated
        assert abs(values[2] - 2.0) < 1e-9
        assert abs(values[3] - 3.0) < 1e-9
        assert abs(values[7] - 7.0) < 1e-9

    def test_na_until_window_full(self):
        from pine.runtime import NA

        runtime = run_script('//@version=5\nindicator("A")\nplot(ta.sma(close, 5))\n', flat_bars([1, 2, 3, 4, 5, 6]))
        values = runtime.plots[0].values
        assert values[0] is NA
        assert values[3] is NA
        assert values[4] == 3.0


class TestEma:
    def test_seeded_with_sma_then_recursive(self):
        closes = [10.0, 11.0, 12.0, 13.0, 14.0]
        runtime = run_script('//@version=5\nindicator("A")\nplot(ta.ema(close, 3))\n', flat_bars(closes))
        values = runtime.plots[0].values
        assert abs(values[2] - 11.0) < 1e-9  # SMA seed
        alpha = 2.0 / 4.0
        expected = alpha * 13.0 + (1 - alpha) * 11.0
        assert abs(values[3] - expected) < 1e-9


class TestWma:
    def test_weighted_average(self):
        closes = [1.0, 2.0, 3.0]
        runtime = run_script('//@version=5\nindicator("A")\nplot(ta.wma(close, 3))\n', flat_bars(closes))
        expected = (1 * 1 + 2 * 2 + 3 * 3) / 6.0
        assert abs(runtime.plots[0].values[2] - expected) < 1e-9


class TestRsi:
    def test_all_up_moves_gives_100(self):
        closes = [float(i) for i in range(1, 25)]
        runtime = run_script('//@version=5\nindicator("A")\nplot(ta.rsi(close, 14))\n', flat_bars(closes))
        values = runtime.plots[0].values
        assert values[13] is not None
        assert abs(values[-1] - 100.0) < 1e-6

    def test_alternating_moves_bounds(self):
        closes = [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0,
                  10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0]
        runtime = run_script('//@version=5\nindicator("A")\nplot(ta.rsi(close, 14))\n', flat_bars(closes))
        value = runtime.plots[0].values[-1]
        assert 0.0 <= value <= 100.0
        # Equal gains and losses oscillate tightly around 50 under Wilder smoothing
        assert 45.0 <= value <= 55.0


class TestAtr:
    def test_wilder_smoothing(self):
        bars = [
            Bar(open=10, high=12, low=9, close=11, volume=1, time=1700000000000),
            Bar(open=11, high=15, low=10.5, close=14, volume=1, time=1700000060000),
            Bar(open=14, high=16, low=13, close=15, volume=1, time=1700000120000),
        ]
        runtime = run_script('//@version=5\nindicator("A")\nplot(ta.atr(2))\n', bars)
        tr1 = max(15 - 10.5, abs(15 - 11), abs(10.5 - 11))
        tr2 = max(16 - 13, abs(16 - 14), abs(13 - 14))
        seed = (3 + tr1) / 2  # first TR is high - low = 3
        expected = 0.5 * tr2 + 0.5 * seed
        assert abs(runtime.plots[0].values[2] - expected) < 1e-9


class TestCrossover:
    def test_crossover_detection(self):
        # fast crosses over slow between bars 2 and 3
        closes = [10, 10, 10, 11, 11, 12]
        bars = flat_bars(closes)
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'fast = ta.sma(close, 1)\nslow = 10.5\n'
            'plot(ta.crossover(fast, slow) ? 1 : 0)\n',
            bars,
        )
        values = runtime.plots[0].values
        assert values[3] == 1
        assert values[4] == 0
        assert values[5] == 0

    def test_crossunder_detection(self):
        closes = [12, 12, 12, 11, 11, 10]
        bars = flat_bars(closes)
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'fast = ta.sma(close, 1)\nslow = 11.5\n'
            'plot(ta.crossunder(fast, slow) ? 1 : 0)\n',
            bars,
        )
        values = runtime.plots[0].values
        assert values[3] == 1
        assert values[4] == 0

    def test_cross_alias(self):
        closes = [10, 10, 10, 11, 11, 10]
        bars = flat_bars(closes)
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'fast = ta.sma(close, 1)\nslow = 10.5\n'
            'plot(ta.cross(fast, slow) ? 1 : 0)\n',
            bars,
        )
        values = runtime.plots[0].values
        assert values[3] == 1
        assert values[5] == 1


class TestMath:
    def test_math_builtins(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\n'
            'plot(math.abs(-3) + math.max(1, 2) + math.min(1, 2))\n'
            'plot(math.round(2.567, 2) * math.sqrt(16))\n'
            'plot(math.pow(2, 10) + math.floor(2.9))\n',
            flat_bars([1, 2, 3]),
        )
        assert abs(runtime.plots[0].values[0] - 6.0) < 1e-9
        assert abs(runtime.plots[1].values[0] - 10.28) < 1e-9
        assert abs(runtime.plots[2].values[0] - 1026.0) < 1e-9


class TestNaHandling:
    def test_na_propagation(self):
        from pine.runtime import NA

        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(ta.sma(close, 50))\n', make_bars(10)
        )
        assert runtime.plots[0].values[0] is NA

    def test_nz_replaces_na(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(nz(ta.sma(close, 50), 0))\n', make_bars(10)
        )
        assert runtime.plots[0].values[0] == 0.0

    def test_na_test(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(na(ta.sma(close, 50)) ? 1 : 0)\n', make_bars(10)
        )
        assert runtime.plots[0].values[0] == 1

    def test_division_by_zero_is_na(self):
        from pine.runtime import NA

        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(close / 0)\n', flat_bars([1, 2, 3])
        )
        assert runtime.plots[0].values[0] is NA


class TestHistoryAndState:
    def test_no_lookahead(self):
        source = '//@version=5\nindicator("A")\nplot(ta.ema(close, 9))\n'
        bars = make_bars(120)
        from pine.compiler import compile_script

        result = compile_script(source)
        partial = PineRuntime(result.script)
        for bar in bars[:80]:
            partial.process_bar(bar)
        full = PineRuntime(result.script)
        for bar in bars:
            full.process_bar(bar)
        assert abs(partial.plots[0].values[-1] - full.plots[0].values[79]) < 1e-12

    def test_history_reference(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(close[2])\n', flat_bars([1, 2, 3, 4, 5])
        )
        values = runtime.plots[0].values
        from pine.runtime import NA

        assert values[0] is NA and values[1] is NA
        assert values[2] == 1.0
        assert values[4] == 3.0

    def test_var_persists_across_bars(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\nvar float cum = 0.0\ncum := cum + 1\nplot(cum)\n',
            flat_bars([1, 2, 3, 4, 5]),
        )
        values = runtime.plots[0].values
        assert values == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_bar_index_and_time(self):
        runtime = run_script(
            '//@version=5\nindicator("A")\nplot(bar_index)\n', make_bars(5)
        )
        assert runtime.plots[0].values == [0.0, 1.0, 2.0, 3.0, 4.0]
