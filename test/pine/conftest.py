"""Shared fixtures for Pine engine tests."""

import sys
from pathlib import Path

import pytest

# Make the repo root importable when the suite is run from elsewhere.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pine_test_utils import make_bars  # noqa: E402

from pine.compiler import CompileResult, compile_script  # noqa: E402
from pine.runtime import Bar  # noqa: E402


@pytest.fixture
def bars() -> list[Bar]:
    return make_bars(300)


@pytest.fixture
def compile():
    """Expose compile_script with an assert-friendly wrapper."""

    def _compile(source: str) -> CompileResult:
        result = compile_script(source)
        assert result.ok, (
            f"expected compile success, got {result.error.to_dict() if result.error else None}"
        )
        return result

    return _compile
