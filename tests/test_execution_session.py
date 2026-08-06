"""Execution continuity tests for issue #258: checkpointing and resume."""

from __future__ import annotations

from verdict.execution_session import ExecutionSession, ExecutionSessionError, FailureEntry
from verdict.memory_plane import MemoryPlane

TASK = {"task": "resolve routing", "requirements": {"required": ["tools"]}}


def _plane(tmp_path: object) -> MemoryPlane:
    return MemoryPlane(str(tmp_path) + "/plane.db")


def _new_session(plane: MemoryPlane, session_id: str = "s1") -> ExecutionSession:
    return ExecutionSession.create(
        session_id,
        TASK,
        steps=[("step_1", "qualify"), ("step_2", "route"), ("step_3", "record")],
        plane=plane,
        model_id="provider/model-a",
    )


def test_create_persists_initial_checkpoint(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    assert session.state == "created"
    assert len(session.checkpoints) == 1
    assert [step.status for step in session.steps] == ["pending", "pending", "pending"]
    assert plane.get("execution_sessions", session.session_id) is not None


def test_checkpoint_after_each_step_advances_state(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    assert session.state == "running"

    session.complete_step(plane, "step_1", model_id="provider/model-a", tokens_used=10)
    assert session.state == "checkpointed"
    assert session.current_step == "step_2"
    assert session.completed_steps == ["step_1"]
    assert [c.seq for c in session.checkpoints] == [1, 2, 3]

    session.complete_step(plane, "step_2")
    session.complete_step(plane, "step_3")
    assert session.state == "completed"
    assert session.current_step is None
    assert session.completed_steps == ["step_1", "step_2", "step_3"]
    assert [c.reason for c in session.checkpoints] == [
        "created",
        "started",
        "step_completed:step_1",
        "step_completed:step_2",
        "step_completed:step_3",
    ]


def test_resume_restores_exact_state_after_crash(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.complete_step(plane, "step_1", model_id="provider/model-a", tokens_used=10)
    session.record_artifact("analysis", {"score": 0.9})
    session.checkpoint(plane, reason="tool_use")

    resumed = ExecutionSession.resume(session.session_id, plane)

    assert resumed.session_id == session.session_id
    assert resumed.state == session.state
    assert resumed.current_step == session.current_step
    assert resumed.completed_steps == session.completed_steps
    assert [step.status for step in resumed.steps] == ["completed", "pending", "pending"]
    assert resumed.artifacts == session.artifacts
    assert resumed.model_id == "provider/model-a"
    # A new checkpoint on top of the restored state does not rewrite history.
    assert len(resumed.checkpoints) == len(session.checkpoints)


def test_resume_unknown_session_raises(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    try:
        ExecutionSession.resume("missing", plane)
        raise AssertionError("expected ExecutionSessionError")
    except ExecutionSessionError as exc:
        assert "no checkpoint" in str(exc)


def test_resume_continues_remaining_steps_without_rerunning(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.complete_step(plane, "step_1")

    resumed = ExecutionSession.resume(session.session_id, plane)
    # The completed step's status and timestamp survive the round trip.
    assert resumed.steps[0].status == "completed"
    assert resumed.steps[0].completed_at is not None
    assert resumed.current_step == "step_2"

    resumed.complete_step(plane, "step_2")
    resumed.complete_step(plane, "step_3")
    assert resumed.state == "completed"
    assert resumed.completed_steps == ["step_1", "step_2", "step_3"]


def test_fail_step_then_resume_from_failure_rearms_only_failed_step(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.complete_step(plane, "step_1")
    session.fail_step(plane, "step_2", error="HTTP 429")

    assert session.state == "failed"
    assert session.current_step == "step_2"
    assert session.steps[1].status == "failed"
    assert session.completed_steps == ["step_1"]

    failure = FailureEntry(
        step_id="step_2",
        provider="provider",
        model_id="model-a",
        error_class="rate_limited",
        message="HTTP 429",
        created_at=session.updated_at,
        status_code=429,
    )
    session.resume_from_failure(plane, failure=failure, replacement_model="model-b")

    assert session.state == "running"
    assert session.steps[1].status == "pending"
    assert session.steps[1].error is None
    assert session.model_id == "model-b"
    assert session.attempts == {"step_2": 1}
    assert len(session.failure_log) == 1
    # step_1 remains completed; only the failed step is re-armed.
    assert session.steps[0].status == "completed"
    assert session.completed_steps == ["step_1"]
    assert session.current_step == "step_2"


def test_serialization_round_trip(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.complete_step(plane, "step_1", model_id="provider/model-a", tokens_used=7)
    payload = session.to_dict()
    restored = ExecutionSession.from_dict(payload)
    assert restored.to_dict() == session.to_dict()
    assert restored.steps[0].tokens_used == 7
    assert restored.steps[0].model_id == "provider/model-a"


def test_side_effect_fields_round_trip(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.steps[0].side_effect_kind = "irreversible"
    session.steps[0].committed = True
    payload = session.to_dict()
    restored = ExecutionSession.from_dict(payload)
    assert restored.steps[0].side_effect_kind == "irreversible"
    assert restored.steps[0].committed is True
    assert restored.to_dict() == session.to_dict()


def test_side_effect_defaults_preserve_existing_behavior(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    assert session.steps[0].side_effect_kind == "read-only"
    assert session.steps[0].committed is False
    # A read-only step serializes without the new fields and round-trips to the
    # same defaults, so previously persisted checkpoints stay valid.
    restored = ExecutionSession.from_dict(session.to_dict())
    assert restored.steps[0].side_effect_kind == "read-only"
    assert restored.steps[0].committed is False


def test_irreversible_committed_step_raises_on_resume(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.steps[0].side_effect_kind = "irreversible"
    session.steps[0].committed = True
    failure = FailureEntry(
        step_id="step_1",
        provider="provider",
        model_id="model-a",
        error_class="crash",
        message="crash after commit",
        created_at=session.updated_at,
    )
    try:
        session.resume_from_failure(plane, failure=failure)
        raise AssertionError("expected ExecutionSessionError")
    except ExecutionSessionError as exc:
        assert "already committed" in str(exc)


def test_committed_durable_effect_not_duplicated_on_resume(tmp_path: object) -> None:
    plane = _plane(tmp_path)
    session = _new_session(plane)
    session.start(plane)
    session.steps[0].side_effect_kind = "reversible"
    session.steps[0].committed = True
    failure = FailureEntry(
        step_id="step_1",
        provider="provider",
        model_id="model-a",
        error_class="crash",
        message="crash after commit",
        created_at=session.updated_at,
    )
    session.resume_from_failure(plane, failure=failure)
    # The step stays completed (skipped, not re-armed) and the cursor moves to
    # the next pending step instead of re-running step_1.
    assert session.steps[0].status == "completed"
    assert session.steps[0].committed is True
    assert session.current_step == "step_2"
    assert session.completed_steps == ["step_1"]
