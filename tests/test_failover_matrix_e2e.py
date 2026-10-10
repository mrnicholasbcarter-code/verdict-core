"""BOD-268: worker failure-isolation matrix through the real WorkerController.

Each scenario drives the production ``WorkerController.run`` loop (ranking,
health cache, classification, exclusion, reaping, replacement) with a
credential-free adapter. Only the Prime spawn/collect boundary is faked.

Candidates rank as ``cc/a``, ``cc/b`` (same provider), then ``kr/c``. The
active controller ``kr/ctrl`` is visible and admitted-looking but must never be
spawned as a worker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import WorkerTerminal as OrchTerminal
from verdict.orchestration.recovery import FailureIntelligence
from verdict.subagent_selection import (
    CONTEXT_LENGTH_CATEGORY,
    HealthCache,
    HealthResult,
    WorkerTask,
    WorkerTerminal,
    classify_worker_failure,
)
from verdict.worker_runtime import RuntimeBudget, WorkerController

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
CONTROLLER = "kr/ctrl"
KIRO_CONTEXT_400 = "[kiro/claude-opus-5.5] [400]: Input is too long. (reset after 84h 5m 18s)"


@pytest.fixture(autouse=True)
def _uniform_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalise kr to the same capacity class as cc so ranking is stable."""
    monkeypatch.setenv("VERDICT_CAPACITY_CLASSES", "kr=claude_subscription")


def _row(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "context_length": 200_000,
        "capabilities": {"tool_calling": True, "reasoning": True},
        "pricing": {"input": 0.0, "output": 0.0},
    }


ROWS = [_row("cc/a"), _row("cc/b"), _row("kr/c"), _row(CONTROLLER)]
VISIBLE = [f"omniroute/{row['id']}" for row in ROWS]


class _Adapter:
    """Prime boundary fake: ``failures`` maps a selector to how it fails."""

    def __init__(self, failures: dict[str, Any]) -> None:
        self.failures = failures
        self.spawned: list[str] = []
        self.prompts: list[str] = []
        self.deleted: list[str] = []

    async def spawn(
        self, prompt: str, *, name: str, model: str, thinking: str | None = None
    ) -> dict[str, Any]:
        self.spawned.append(model)
        self.prompts.append(prompt)
        return {"rlm_child_id": name, "model": model}

    async def collect(self, handle: dict[str, Any]) -> Any:
        failure = self.failures.get(str(handle["model"]))
        if failure is None:
            return WorkerTerminal("done", "WORKER_OK", True, stop_reason="stop")
        if isinstance(failure, BaseException):
            raise failure
        return failure  # a (possibly malformed) terminal value

    async def delete(self, handle: dict[str, Any]) -> None:
        self.deleted.append(str(handle["model"]))


def _controller(adapter: _Adapter, cache: HealthCache, **budget: Any) -> WorkerController:
    return WorkerController(
        WorkerTask(
            required_capabilities=frozenset({"tools"}),
            allowed_route_prefixes=frozenset({"cc/", "kr/"}),
            excluded_route_ids=frozenset({CONTROLLER}),
        ),
        inventory_rows=ROWS,
        prime_selectors=VISIBLE,
        probe=lambda candidate: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=cache,
        budget=RuntimeBudget(**budget),
        now=lambda: NOW,
    )


def _error(text: str) -> WorkerTerminal:
    return WorkerTerminal("error", error=text)


# (id, failure on cc/a, classification, provider_wide, expected spawn order)
MATRIX = [
    ("400-unsupported", _error("HTTP 400 unsupported parameter"), "unsupported", False, "b"),
    ("401", _error("HTTP 401 invalid api key"), "authentication", True, "c"),
    ("402", _error("HTTP 402 payment required"), "payment_required", True, "c"),
    ("403", _error("HTTP 403 provider usage unavailable"), "permission", True, "c"),
    ("429", _error("HTTP 429 rate limited; retry-after: 30"), "rate_limited", True, "c"),
    ("5xx", _error("HTTP 503 upstream unavailable"), "upstream_temporary", False, "b"),
    ("timeout", TimeoutError("child execution deadline"), "timeout", False, "b"),
    ("malformed", {"not": "a terminal"}, "malformed_result", False, "b"),
    ("empty", WorkerTerminal("done", "   ", True, stop_reason="stop"), "empty_output", False, "b"),
    ("context-400", _error(KIRO_CONTEXT_400), CONTEXT_LENGTH_CATEGORY, False, "b"),
]


@pytest.mark.parametrize(
    ("failure", "category", "provider_wide", "next_route"),
    [pytest.param(*row[1:], id=row[0]) for row in MATRIX],
)
def test_worker_failure_is_isolated_and_replaced(
    tmp_path: Path, failure: Any, category: str, provider_wide: bool, next_route: str
) -> None:
    adapter = _Adapter({"omniroute/cc/a": failure})
    cache = HealthCache(tmp_path / "health.json")
    runtime = _controller(adapter, cache)

    # The parent operation returns an outcome; it never raises.
    outcome = asyncio.run(runtime.run("same immutable task"))

    replacement = "omniroute/cc/b" if next_route == "b" else "omniroute/kr/c"
    assert outcome.state == "SUCCESS", outcome.diagnostic
    assert outcome.candidate is not None and outcome.candidate.selector == replacement
    # Exact attempt order; explicit model= on every spawn; controller never a worker.
    assert adapter.spawned == ["omniroute/cc/a", replacement]
    assert f"omniroute/{CONTROLLER}" not in adapter.spawned
    # Same immutable task to the replacement; the failed writer is reaped first.
    assert adapter.prompts == ["same immutable task", "same immutable task"]
    assert adapter.deleted[0] == "omniroute/cc/a"
    assert outcome.attempts[0] == ("omniroute/cc/a", category)

    failure_event = next(e for e in runtime.events if e.get("event") == "failure")
    assert failure_event["classification"] == category
    assert failure_event["provider_wide"] is provider_wide
    assert failure_event["excluded"] is True
    assert failure_event["model"] == "omniroute/cc/a"
    selection = [e for e in runtime.events if e.get("event") == "selection"]
    assert [e["model"] for e in selection] == ["omniroute/cc/a", replacement]
    assert selection[-1]["replacement_model"] == replacement

    if provider_wide:
        skipped = [
            e
            for e in runtime.events
            if e.get("event") == "exclusion" and e.get("model") == "omniroute/cc/b"
        ]
        assert skipped and skipped[0]["provider_wide"] is True

    persisted = (
        json.loads((tmp_path / "health.json").read_text())
        if (tmp_path / "health.json").exists()
        else {}
    )
    records = persisted.get("records", persisted)
    if category == CONTEXT_LENGTH_CATEGORY:
        # Request-scoped: no cooldown on the route or its provider.
        assert failure_event["cooldown_seconds"] == 0
        cached = cache.usable("omniroute/cc/a", now=NOW)
        assert cached is None or cached.healthy is True
        assert "provider:cc" not in records
        assert all(record.get("healthy", True) for record in records.values())
    else:
        assert failure_event["cooldown_seconds"] > 0
        cached = cache.usable("omniroute/cc/a", now=NOW)
        assert cached is not None and cached.healthy is False
        assert cached.category == category


def test_pool_exhaustion_fails_closed_without_using_the_controller(tmp_path: Path) -> None:
    adapter = _Adapter(
        {
            "omniroute/cc/a": _error("HTTP 503 upstream unavailable"),
            "omniroute/cc/b": _error("HTTP 503 upstream unavailable"),
            "omniroute/kr/c": _error("HTTP 429 rate limited"),
        }
    )
    runtime = _controller(adapter, HealthCache(tmp_path / "health.json"))

    outcome = asyncio.run(runtime.run("task"))

    assert outcome.state == "FAIL_CLOSED"
    assert adapter.spawned == ["omniroute/cc/a", "omniroute/cc/b", "omniroute/kr/c"]
    assert f"omniroute/{CONTROLLER}" not in adapter.spawned
    finals = [e for e in runtime.events if e.get("event") == "final"]
    assert len(finals) == 1


def test_replacement_budget_is_bounded(tmp_path: Path) -> None:
    adapter = _Adapter(
        {
            "omniroute/cc/a": _error("HTTP 503 upstream unavailable"),
            "omniroute/cc/b": _error("HTTP 503 upstream unavailable"),
        }
    )
    runtime = _controller(adapter, HealthCache(tmp_path / "health.json"), max_attempts=2)

    outcome = asyncio.run(runtime.run("task"))

    assert outcome.state == "FAIL_CLOSED"
    assert "replacement budget exhausted" in outcome.diagnostic
    assert adapter.spawned == ["omniroute/cc/a", "omniroute/cc/b"]


def test_context_length_classification_ignores_quota_reset_text() -> None:
    worker = classify_worker_failure(RuntimeError(KIRO_CONTEXT_400))
    assert worker.category == CONTEXT_LENGTH_CATEGORY
    assert worker.retry_after_seconds is None

    orch = FailureIntelligence().classify(
        OrchTerminal(ok=False, model="kr/claude-opus-5.5", error=KIRO_CONTEXT_400, status_code=400),
        now=NOW,
    )
    assert orch.category == CONTEXT_LENGTH_CATEGORY
    assert orch.scope == "none"
    assert orch.cooldown_seconds == 0
    assert orch.action == "REROUTE"


def test_context_overflow_skips_routes_that_cannot_fit_and_uses_a_larger_window(
    tmp_path: Path,
) -> None:
    """BOD-272: no blind replay of an oversized request onto an equal/smaller window."""
    rows = [
        {**_row("cc/a"), "context_length": 200_000},
        {**_row("cc/b"), "context_length": 128_000},
        {**_row("kr/c"), "context_length": 1_000_000},
    ]
    visible = [f"omniroute/{r['id']}" for r in rows]
    adapter = _Adapter({"omniroute/cc/a": _error(KIRO_CONTEXT_400)})
    runtime = WorkerController(
        WorkerTask(
            required_capabilities=frozenset({"tools"}),
            allowed_route_prefixes=frozenset({"cc/", "kr/"}),
        ),
        inventory_rows=rows,
        prime_selectors=visible,
        probe=lambda candidate: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=HealthCache(tmp_path / "health.json"),
        now=lambda: NOW,
    )

    outcome = asyncio.run(runtime.run("oversized task"))

    assert outcome.state == "SUCCESS", outcome.diagnostic
    ranked = [c.selector for c in runtime.candidates]
    first = ranked[0]
    assert first == "omniroute/cc/a"
    # The smaller-window sibling is skipped with a named reason, never spawned.
    assert "omniroute/cc/b" not in adapter.spawned
    skipped = [
        e
        for e in runtime.events
        if e.get("event") == "exclusion" and e.get("model") == "omniroute/cc/b"
    ]
    assert skipped and skipped[0]["classification"] == "context_too_small"
    assert skipped[0]["overflowed_at"] == 200_000
    # The larger-window route receives the same immutable prompt.
    assert adapter.spawned == ["omniroute/cc/a", "omniroute/kr/c"]
    assert adapter.prompts == ["oversized task", "oversized task"]
