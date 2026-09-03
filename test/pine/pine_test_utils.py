"""Shared helpers for Pine engine tests (sibling module import)."""

from pine.runtime import Bar


def make_bars(count: int, seed: int = 42, start_price: float = 100.0) -> list[Bar]:
    """Deterministic pseudo-random walk candles for repeatable tests."""
    import random

    rng = random.Random(seed)
    bars: list[Bar] = []
    price = start_price
    base_time = 1700000000000
    for i in range(count):
        drift = 0.3 if (i // 40) % 2 == 0 else -0.3
        o = price
        c = o + drift + rng.uniform(-0.4, 0.4)
        h = max(o, c) + rng.uniform(0, 0.3)
        low = min(o, c) - rng.uniform(0, 0.3)
        bars.append(
            Bar(open=o, high=h, low=low, close=c, volume=1000.0 + i, time=base_time + i * 60000)
        )
        price = c
    return bars


def trend_bars(directions=(1, -1, 1), length=60, step=0.8) -> list[Bar]:
    """Segmented trend bars: up, down, up... deterministic and crossable."""
    bars: list[Bar] = []
    price = 100.0
    t = 1700000000000
    idx = 0
    for direction in directions:
        for _ in range(length):
            o = price
            c = o + direction * step
            h = max(o, c) + 0.1
            low = min(o, c) - 0.1
            bars.append(Bar(open=o, high=h, low=low, close=c, volume=100.0, time=t + idx * 60000))
            price = c
            idx += 1
    return bars
