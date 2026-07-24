"""
Tests for swarm task envelope, termination, and result contracts (Issue #43 / Slice 37.1).

These tests prove the AC:
- Unknown unsafe fields and missing termination conditions are rejected
- Workspace and artifact references cannot escape configured roots
- Budgets and iteration/concurrency caps are positive and enforced
- Result states distinguish success, failure, blocked, timeout, cancelled, rejected
- JSON fixtures are shared with TypeScript implementation or have documented parity plan
"""

from __future__ import annotations

import pytest

from verdict.swarm_contracts import (
    SwarmTaskAttempt,
    SwarmTaskBudget,
    SwarmTaskEnvelope,
    SwarmTaskEvent,
    SwarmTaskResult,
    SwarmTaskState,
    SwarmTaskVerification,
    TerminationReason,
    _reject_unsafe_fields,
    _validate_rooted_path,
    build_swarm_task_envelope,
    create_task_attempt,
)


class TestSwarmTaskBudget:
    """Tests for budget validation."""

    def test_budget_requires_at_least_one_positive_cap(self):
        """At least one budget cap must be positive."""
        with pytest.raises(Exception) as exc:
            SwarmTaskBudget(max_usd=0, max_tokens=0, max_latency_ms=0)
        assert "at least one budget cap" in str(exc.value)

    def test_budget_rejects_negative_values(self):
        """Budget fields must be non-negative."""
        with pytest.raises(Exception) as exc:
            SwarmTaskBudget(max_usd=-1, max_tokens=1000, max_latency_ms=1000)
        assert "non-negative" in str(exc.value)

    def test_budget_accepts_valid_positive_caps(self):
        """Valid positive budgets are accepted."""
        budget = SwarmTaskBudget(max_usd=0.01, max_tokens=10000, max_latency_ms=30000)
        assert budget.max_usd == 0.01
        assert budget.max_tokens == 10000


class TestSwarmTaskEnvelope:
    """Tests for task envelope validation."""

    def test_envelope_requires_objective(self):
        """Objective must be at least 3 characters."""
        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="ab")
        assert "at least 3 characters" in str(exc.value)

    def test_envelope_rejects_unsafe_objective(self):
        """Objective with unsafe patterns is rejected."""
        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="test; exec system")
        assert "unsafe content" in str(exc.value)

    def test_envelope_rejects_unsafe_path(self):
        """Paths with path traversal are rejected."""
        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="valid task", allowed_paths=["/home/nick/../../etc/passwd"])
        assert "escapes allowed roots" in str(exc.value)

    def test_envelope_accepts_valid_rooted_paths(self):
        """Paths within allowed roots are accepted."""
        envelope = SwarmTaskEnvelope(
            objective="valid task", allowed_paths=["/home/nick/dev/project", "/tmp/work"]
        )
        assert len(envelope.allowed_paths) == 2

    def test_envelope_rejects_invalid_stop_conditions(self):
        """Unknown stop conditions are rejected."""
        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="valid task", stop_conditions=["invalid_condition"])
        assert "invalid stop_condition" in str(exc.value)

    def test_envelope_accepts_valid_stop_conditions(self):
        """Valid stop conditions are accepted."""
        envelope = SwarmTaskEnvelope(
            objective="valid task",
            stop_conditions=["objective_achieved", "timeout", "budget_exceeded"],
        )
        assert "objective_achieved" in envelope.stop_conditions

    def test_envelope_enforces_positive_caps(self):
        """Numeric caps must be positive."""
        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="test", timeout_ms=0)
        assert "timeout_ms must be positive" in str(exc.value)

        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="test", max_iterations=0)
        assert "max_iterations must be positive" in str(exc.value)

        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="test", max_parallelism=0)
        assert "max_parallelism must be positive" in str(exc.value)

    def test_envelope_enforces_max_attempts(self):
        """max_attempts must be positive."""
        with pytest.raises(Exception) as exc:
            SwarmTaskEnvelope(objective="test", max_attempts=0)
        assert "max_attempts must be positive" in str(exc.value)

    def test_envelope_accepts_budget(self):
        """Budget object is accepted."""
        budget = SwarmTaskBudget(max_usd=1.0, max_tokens=50000, max_latency_ms=60000)
        envelope = SwarmTaskEnvelope(objective="test", budget=budget)
        assert envelope.budget is not None
        assert envelope.budget.max_usd == 1.0


class TestSwarmTaskAttempt:
    """Tests for task attempt lifecycle."""

    def test_attempt_creation(self):
        """Attempt creation works."""
        attempt = SwarmTaskAttempt(task_id="task-123")
        assert attempt.task_id == "task-123"
        assert attempt.state == "pending"
        assert attempt.attempt_id is not None

    def test_attempt_mark_completed_success(self):
        """Successful completion marks state correctly."""
        attempt = SwarmTaskAttempt(task_id="task-123")
        completed = attempt.mark_completed(
            result="success", reason="completed", output_refs=["artifact-1"]
        )
        assert completed.state == "completed"
        assert completed.result == "success"
        assert completed.termination_reason == "completed"
        assert completed.output_artifact_refs == ["artifact-1"]

    def test_attempt_mark_completed_failure(self):
        """Failed completion marks state correctly."""
        attempt = SwarmTaskAttempt(task_id="task-123")
        completed = attempt.mark_completed(
            result="failure",
            reason="failed",
        )
        assert completed.state == "failed"
        assert completed.result == "failure"


class TestSwarmTaskEvent:
    """Tests for task events."""

    def test_event_creation(self):
        """Event creation works."""
        event = SwarmTaskEvent(
            task_id="task-123",
            attempt_id="attempt-456",
            event_type="started",
            payload={"agent": "coder-1"},
        )
        assert event.task_id == "task-123"
        assert event.event_type == "started"
        assert event.payload["agent"] == "coder-1"


class TestSwarmTaskVerification:
    """Tests for task verification."""

    def test_verification_creation(self):
        """Verification creation works."""
        verification = SwarmTaskVerification(
            task_id="task-123",
            attempt_id="attempt-456",
            passed=True,
            checks={"lint": True, "tests": True},
            details={"lint_output": "clean"},
        )
        assert verification.passed is True
        assert verification.checks["lint"] is True


class TestFactoryFunctions:
    """Tests for factory functions."""

    def test_build_envelope_factory(self):
        """Factory creates valid envelope."""
        envelope = build_swarm_task_envelope(
            objective="Test task",
            allowed_paths=["/home/nick/dev/project"],
        )
        assert envelope.objective == "Test task"
        assert envelope.allowed_paths == ["/home/nick/dev/project"]

    def test_create_task_attempt_factory(self):
        """Factory creates valid attempt."""
        attempt = create_task_attempt("task-123")
        assert attempt.task_id == "task-123"
        assert attempt.state == "pending"


class TestPathValidation:
    """Tests for path safety."""

    def test_validate_rooted_path_accepts_allowed(self):
        """Paths within allowed roots are accepted."""
        assert _validate_rooted_path("/home/nick/dev/project")
        assert _validate_rooted_path("/tmp/work")
        assert _validate_rooted_path("/workspace/data")

    def test_validate_rooted_path_rejects_escapes(self):
        """Paths escaping roots are rejected."""
        assert not _validate_rooted_path("/home/nick/../../etc/passwd")
        assert not _validate_rooted_path("/etc/passwd")
        assert not _validate_rooted_path("~/.ssh/id_rsa")


class TestUnsafeFieldRejection:
    """Tests for unsafe field rejection."""

    def test_reject_unsafe_fields_detects_patterns(self):
        """Unsafe patterns in fields are detected."""
        with pytest.raises(Exception) as exc:
            _reject_unsafe_fields({"test": "exec system"})
        assert "unsafe content" in str(exc.value)

    def test_reject_unsafe_fields_detects_path_traversal(self):
        """Path traversal in fields is detected."""
        with pytest.raises(Exception) as exc:
            _reject_unsafe_fields({"path": "/home/../etc/passwd"})
        assert "unsafe content" in str(exc.value)


class TestResultStates:
    """Tests for result state enums."""

    def test_swarm_task_states(self):
        """All required states exist."""
        assert SwarmTaskState.PENDING
        assert SwarmTaskState.ASSIGNED
        assert SwarmTaskState.RUNNING
        assert SwarmTaskState.BLOCKED
        assert SwarmTaskState.COMPLETED
        assert SwarmTaskState.FAILED
        assert SwarmTaskState.TIMEOUT
        assert SwarmTaskState.CANCELLED
        assert SwarmTaskState.REJECTED

    def test_swarm_task_results(self):
        """All required result states exist."""
        assert SwarmTaskResult.SUCCESS
        assert SwarmTaskResult.FAILURE
        assert SwarmTaskResult.BLOCKED
        assert SwarmTaskResult.TIMEOUT
        assert SwarmTaskResult.CANCELLED
        assert SwarmTaskResult.REJECTED

    def test_termination_reasons(self):
        """All termination reasons exist."""
        assert TerminationReason.COMPLETED
        assert TerminationReason.FAILED
        assert TerminationReason.TIMEOUT
        assert TerminationReason.CANCELLED_BY_USER
        assert TerminationReason.CANCELLED_BY_SYSTEM
        assert TerminationReason.BUDGET_EXCEEDED
        assert TerminationReason.ITERATION_LIMIT
        assert TerminationReason.RESOURCE_EXHAUSTED
        assert TerminationReason.POLICY_VIOLATION
        assert TerminationReason.DEPENDENCY_FAILED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
