"""Real session feedback changes subsequent selection without poisoning task failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.contracts import (
    NodeKind,
    NodeState,
    TaskRequirements,
    WorkerTerminal,
    WorkNode,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.receipt import EventLog
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, NodeRun
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
A = "cc/claude-sonnet-5"
B = "cc/claude-haiku-5"
C = "cx/gpt-6-codex"


def ladder(tmp_path: Path, state_name: str = "ladder.json") -> EligibilityLadder:
    rows = [
        {
            "id": rid,
            "owned_by": provider,
            "context_length": 200000,
            "capabilities": {"tool_calling": True, "reasoning": True},
            "pricing": {"input": 1, "output": 2},
        }
        for rid, provider in [(A, "claude"), (B, "claude"), (C, "codex")]
    ]
    connections = [
        {
            "provider": p,
            "authType": "oauth",
            "isActive": True,
            "testStatus": "ok",
            "plan_label": "max",
        }
        for p in ["claude", "codex"]
    ]
    return EligibilityLadder(
        rows,
        connections,
        lambda _: HealthResult(True, ""),
        tmp_path / state_name,
        health_cache=HealthCache(tmp_path / "health.json"),
    )


def record_runtime_failure(
    tmp_path: Path, selected: EligibilityLadder, terminal: WorkerTerminal
) -> None:
    # Exercise the production handler without unrelated Git/worker execution.
    runtime = object.__new__(DagRuntime)
    runtime.selector = selected
    runtime.classifier = FailureIntelligence()
    runtime.now = lambda: NOW
    runtime.events = EventLog(tmp_path / "events.jsonl")
    runtime.run_dir = tmp_path
    run = NodeRun(
        WorkNode("n1", "build functionality", kind=NodeKind.RESEARCH),
        state=NodeState.RUNNING,
        route_id=A,
        attempt=1,
    )
    runtime._record_failure(run, terminal, [])


def test_live_pool_failure_blocks_siblings_in_new_run(tmp_path: Path) -> None:
    selected = ladder(tmp_path)
    record_runtime_failure(
        tmp_path,
        selected,
        WorkerTerminal(
            ok=False,
            model=A,
            status_code=429,
            error="account quota exhausted",
            executor_kind="live",
        ),
    )
    # Different ladder-state file proves this is the health-cache learning bridge.
    new_run = ladder(tmp_path, "new-run.json")
    choice, verdicts = new_run.select(TaskRequirements(), now=NOW + timedelta(seconds=1))
    assert choice is not None and choice.route_id == C
    assert new_run.dispatch_blocker(B, now=NOW + timedelta(seconds=1)) == "pool:claude"
    assert any(v.route_id == A and v.failed_stage is not None for v in verdicts)


def test_model_quota_does_not_poison_credential_siblings(tmp_path: Path) -> None:
    selected = ladder(tmp_path)
    record_runtime_failure(
        tmp_path,
        selected,
        WorkerTerminal(
            ok=False,
            model=A,
            status_code=429,
            error="quota exceeded for this model",
            executor_kind="live",
        ),
    )
    assert selected.dispatch_blocker(B, now=NOW + timedelta(seconds=1)) is None
    new_run = ladder(tmp_path, "new-run.json")
    choice, _ = new_run.select(TaskRequirements(), now=NOW + timedelta(seconds=1))
    assert choice is not None and choice.route_id == B


def test_expired_negative_requires_new_probe_not_automatic_health(tmp_path: Path) -> None:
    selected = ladder(tmp_path)
    record_runtime_failure(
        tmp_path,
        selected,
        WorkerTerminal(
            ok=False, model=A, status_code=503, error="upstream unavailable", executor_kind="live"
        ),
    )
    fresh_run = ladder(tmp_path, "new-run.json")
    cache = HealthCache(tmp_path / "health.json")
    later = NOW + timedelta(seconds=61)
    assert cache.lookup(A, later).state == "unprobed"
    assert fresh_run._health_status(A, later)[0] == "unprobed"
    choice, _ = fresh_run.select(TaskRequirements(exclude_routes=frozenset({B, C})), now=later)
    assert choice is not None and choice.route_id == A


@pytest.mark.parametrize(
    "error", ["verification_failed: bad code", "ownership_violation: stray file"]
)
def test_task_failure_never_writes_negative_health(tmp_path: Path, error: str) -> None:
    selected = ladder(tmp_path)
    record_runtime_failure(
        tmp_path, selected, WorkerTerminal(ok=False, model=A, error=error, executor_kind="live")
    )
    assert HealthCache(tmp_path / "health.json").entry(A) is None


def test_injected_failure_never_writes_live_learning(tmp_path: Path) -> None:
    selected = ladder(tmp_path)
    record_runtime_failure(
        tmp_path,
        selected,
        WorkerTerminal(
            ok=False,
            model=A,
            status_code=429,
            error="account quota exhausted",
            executor_kind="fault-injected",
        ),
    )
    assert HealthCache(tmp_path / "health.json").entry(A) is None


@pytest.mark.parametrize(
    "status,error", [(503, "upstream unavailable"), (429, "account quota exhausted")]
)
def test_existing_selector_reloads_external_failure_before_dispatch(
    tmp_path: Path, status: int, error: str
) -> None:
    existing = ladder(tmp_path, "existing.json")
    writer = ladder(tmp_path, "writer.json")
    record_runtime_failure(
        tmp_path,
        writer,
        WorkerTerminal(ok=False, model=A, status_code=status, error=error, executor_kind="live"),
    )
    expected = "pool:claude" if status == 429 else f"route:{A}"
    assert existing.dispatch_blocker(A, now=NOW + timedelta(seconds=1)) == expected
    choice, _ = existing.select(TaskRequirements(), now=NOW + timedelta(seconds=1))
    assert choice is not None and choice.route_id != A
    if status == 429:
        assert choice.route_id == C


def test_refresh_negative_cannot_be_overridden_by_cached_positive(tmp_path: Path) -> None:
    from verdict.orchestration.health_cache import ProbeResult

    selected = ladder(tmp_path)
    cache = HealthCache(tmp_path / "health.json")
    cache.record(A, ProbeResult("ok", True, True), NOW - timedelta(seconds=1))
    cache.save()

    def refresh(ids: list[str], now: datetime) -> dict[str, str]:
        writer = ladder(tmp_path, "writer.json")
        record_runtime_failure(
            tmp_path,
            writer,
            WorkerTerminal(
                ok=False,
                model=A,
                status_code=503,
                error="upstream unavailable",
                executor_kind="live",
            ),
        )
        return {}

    selected._refresh_hook = refresh
    choice, _ = selected.select(TaskRequirements(), now=NOW + timedelta(seconds=1))
    assert choice is not None and choice.route_id != A
