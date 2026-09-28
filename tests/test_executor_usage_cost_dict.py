"""Regression: PrimeHeadlessExecutor must accept Prime's real ``usage.cost`` dict.

Observed in the live2 smoke (2026-09-28): prime-agent reports
``usage.cost`` as ``{"input", "output", "cacheRead", "cacheWrite", "total"}``.
``float(dict)`` raised TypeError, so every kr/* worker attempt failed.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from verdict.orchestration.executors import PrimeHeadlessExecutor

REAL_PRIME_USAGE: dict[str, Any] = {
    "input": 6933,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 6933,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def _usage(raw: dict[str, Any]) -> Any:
    return PrimeHeadlessExecutor._extract_usage({"role": "assistant", "usage": raw})


def test_real_prime_cost_dict_does_not_raise_and_uses_total() -> None:
    usage = _usage(REAL_PRIME_USAGE)
    assert usage is not None
    assert usage.input_tokens == 6933
    assert usage.output_tokens == 0
    assert usage.cost_usd == 0.0


def test_cost_dict_nonzero_total() -> None:
    raw = dict(REAL_PRIME_USAGE, cost={"input": 0.001, "output": 0.002, "total": 0.003})
    usage = _usage(raw)
    assert usage is not None
    assert usage.cost_usd == pytest.approx(0.003)


def test_cost_dict_without_total_is_not_fabricated() -> None:
    raw = dict(REAL_PRIME_USAGE, cost={"input": 0.001, "output": 0.002})
    usage = _usage(raw)
    assert usage is not None
    assert usage.cost_usd is None


@pytest.mark.parametrize("bad", ["n/a", [], {"total": "x"}, math.nan, math.inf, True])
def test_malformed_cost_never_raises(bad: Any) -> None:
    usage = _usage(dict(REAL_PRIME_USAGE, cost=bad))
    assert usage is not None
    assert usage.cost_usd is None


def test_scalar_cost_still_supported() -> None:
    usage = _usage({"input": 10, "output": 5, "cost_usd": 0.25})
    assert usage is not None
    assert usage.cost_usd == pytest.approx(0.25)


@pytest.mark.parametrize("bad", ["many", {"n": 1}, [], True])
def test_malformed_token_counts_never_raise(bad: Any) -> None:
    usage = _usage({"input": bad, "output": 7})
    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 7
