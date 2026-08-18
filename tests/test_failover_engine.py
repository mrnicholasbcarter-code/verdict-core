"""Capability-matched failover tests for issue #258.

Injects simulated 429/500/timeout provider failures and proves the engine
quarantines the failing model, selects an equivalent qualified passport,
rebinds the session, and resumes at the failed step without re-running
completed work.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from verdict.execution_session import ExecutionSession
from verdict.failover_engine import (
    FailoverEngine,
    FailoverEngineError,
    is_retryable_status_code,
    is_transient_error_class,
)
from verdict.memory_plane import MemoryPlane
from verdict.model_passports import ModelPassport

TASK = {"task": "resolve routing", "requirements": {"required": ["tools"]}}


def _passport(provider: str, model_id: str, *, tool_support: bool = True) -> ModelPassport:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    return ModelPassport(
        provider=provider,
        model_id=model_id,
        auth_state="authorized",
        context_window=16_000,
        tool_support=tool_support,
        token_cost_per_1k=1.0,
        last_verified_timestamp=now,
        availability_state="eligible",
        qualified_at=now,
        expires_at=now + timedelta(seconds=3600),
    )


def _plane(tmp_path: object) -> MemoryPlane:
    return MemoryPlane(str(tmp_path) + "/plane.db")


def _session(plane: MemoryPlane) -> ExecutionSession:
    session = ExecutionSession.create(
        "s-fail",
        TASK,
        steps=[("qualify", "qualify"), ("route", "route"), ("record", "record")],
        plane=plane,
        model_id="provider-a/model-a",
    )
    session.start(plane)
    session.complete_step(plane, "qualify", model_id="provider-a/model-a")
    return session


def _candidates() -> list[ModelPassport]:
    return [
        _passport("provider-a", "model-b", tool_support=True),
        _passport("provider-b", "model-c", tool_support=True),
    ]


def test_transient_failure_classification() -> None:
    assert is_transient_error_class("rate_limited")
    assert is_transient_error_class("timeout")
    assert not is_transient_error_class("malformed_response")
    assert is_retryable_status_code(429)
    assert is_retryable_status_code(500)
    assert is_retryable_status_code(504)
    assert not is_retryable_status_code(401)


def test_failover_on_429_quarantines_and_rebinds(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()

    engine.failover(
        session,
        plane,
        provider="provider-a",
        model_id="model-a",
        error_class="rate_limited",
        message="HTTP 429",
        status_code=429,
        candidates=_candidates(),
    )

    # Failed model is quarantined; replacement is bound; session resumed.
    assert engine.is_quarantined("provider-a/model-a")
    assert not engine.is_quarantined("provider-b/model-c")
    assert session.model_id == "model-b"
    assert session.bound_passport_key == "provider-a/model-b"
    assert session.state == "running"
    assert session.current_step == "route"
    # Completed step was not re-run.
    assert session.completed_steps == ["qualify"]
    assert session.steps[0].status == "completed"
    # Failed step was re-armed as pending.
    assert session.steps[1].status == "pending"
    assert len(session.failure_log) == 1
    assert session.failure_log[0].status_code == 429
    assert session.attempts == {"route": 1}


def test_resume_completes_remaining_without_duplicating_work(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()
    engine.failover(
        session,
        plane,
        provider="provider-a",
        model_id="model-a",
        error_class="server_error",
        status_code=500,
        candidates=_candidates(),
    )

    # Simulate a crash/restart: rebuild from the persisted checkpoint.
    resumed = ExecutionSession.resume(session.session_id, plane)
    assert resumed.current_step == "route"
    assert resumed.completed_steps == ["qualify"]
    assert resumed.model_id == "model-b"

    resumed.complete_step(plane, "route", model_id="model-b")
    resumed.complete_step(plane, "record", model_id="model-b")
    assert resumed.state == "completed"
    assert resumed.completed_steps == ["qualify", "route", "record"]


def test_failover_on_timeout_switches_provider(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()

    engine.failover(
        session,
        plane,
        provider="provider-a",
        model_id="model-a",
        error_class="timeout",
        candidates=[_passport("provider-a", "model-b"), _passport("provider-b", "model-c")],
    )
    # Same-provider preferred, then lexicographic.
    assert session.model_id == "model-b"
    assert engine.is_quarantined("provider-a/model-a")


def test_non_transient_failure_does_not_fail_over(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()
    with pytest.raises(FailoverEngineError, match="not transient"):
        engine.failover(
            session,
            plane,
            provider="provider-a",
            model_id="model-a",
            error_class="malformed_response",
            status_code=400,
            candidates=_candidates(),
        )
    assert session.model_id == "provider-a/model-a"
    assert session.state == "checkpointed"


def test_no_equivalent_candidate_raises(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()
    with pytest.raises(FailoverEngineError, match="no equivalent qualified model"):
        engine.failover(
            session,
            plane,
            provider="provider-a",
            model_id="model-a",
            error_class="rate_limited",
            status_code=429,
            # Only the failing model is present; no equivalent remains.
            candidates=[_passport("provider-a", "model-a")],
        )


def test_tool_capability_mismatch_excluded(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()
    with pytest.raises(FailoverEngineError, match="no equivalent qualified model"):
        engine.failover(
            session,
            plane,
            provider="provider-a",
            model_id="model-a",
            error_class="rate_limited",
            status_code=429,
            # model-c lacks tool support, so it is not capability-equivalent.
            candidates=[_passport("provider-b", "model-c", tool_support=False)],
        )


def test_quarantined_passport_is_never_selected(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _session(plane)
    engine = FailoverEngine()
    engine.quarantine(_passport("provider-b", "model-c"), error_class="rate_limited")
    with pytest.raises(FailoverEngineError, match="no equivalent qualified model"):
        engine.failover(
            session,
            plane,
            provider="provider-a",
            model_id="model-a",
            error_class="rate_limited",
            status_code=429,
            candidates=[_passport("provider-b", "model-c")],
        )
