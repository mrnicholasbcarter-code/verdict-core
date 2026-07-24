"""
Tests for Ruflo verification gates and bounded replanning (Issue #41 / Slice 32.4).
"""

from __future__ import annotations

import pytest

from verdict.ruflo_verification import (
    FailureClassification,
    ReplanReason,
    ReplanRecord,
    VerificationGate,
    VerificationGateContext,
    VerificationOutcome,
    VerificationResult,
    approve_replan,
    can_replan,
    completion_evidence,
    propose_replan,
    run_verification_gates,
)


class TestVerificationOutcome:
    """Tests for VerificationOutcome enum."""

    def test_outcome_values(self):
        assert VerificationOutcome.PASS.value == "pass"
        assert VerificationOutcome.FAIL.value == "fail"
        assert VerificationOutcome.BLOCKED.value == "blocked"
        assert VerificationOutcome.INCONCLUSIVE.value == "inconclusive"


class TestFailureClassification:
    """Tests for FailureClassification enum."""

    def test_all_classifications(self):
        assert FailureClassification.PROVIDER.value == "provider"
        assert FailureClassification.QUOTA.value == "quota"
        assert FailureClassification.TOOL.value == "tool"
        assert FailureClassification.PERMISSION.value == "permission"
        assert FailureClassification.TEST.value == "test"
        assert FailureClassification.PLAN.value == "plan"
        assert FailureClassification.UNKNOWN.value == "unknown"


class TestVerificationGate:
    """Tests for VerificationGate configuration."""

    def test_default_gate(self):
        gate = VerificationGate(name="test-gate")
        assert gate.name == "test-gate"
        assert gate.required is True
        assert gate.command == ""
        assert gate.timeout_seconds == 60
        assert gate.allowed_failures == 0
        assert gate.requires_approval_on_fail is False

    def test_custom_gate(self):
        gate = VerificationGate(
            name="custom-gate",
            required=False,
            command="pytest tests/",
            timeout_seconds=120,
            allowed_failures=1,
            requires_approval_on_fail=True,
            approval_reason="Flaky tests may need manual review",
        )
        assert gate.required is False
        assert gate.command == "pytest tests/"
        assert gate.timeout_seconds == 120
        assert gate.allowed_failures == 1
        assert gate.requires_approval_on_fail is True
        assert gate.approval_reason == "Flaky tests may need manual review"


class TestVerificationResult:
    """Tests for VerificationResult."""

    def test_pass_result(self):
        result = VerificationResult(
            check_name="test-check",
            outcome=VerificationOutcome.PASS,
            message="All good",
        )
        assert result.outcome == VerificationOutcome.PASS
        assert result.classification is None
        assert result.requires_approval is False

    def test_fail_result_with_classification(self):
        result = VerificationResult(
            check_name="test-check",
            outcome=VerificationOutcome.FAIL,
            classification=FailureClassification.QUOTA,
            message="Budget exceeded",
            requires_approval=True,
            approval_reason="Need budget increase approval",
        )
        assert result.outcome == VerificationOutcome.FAIL
        assert result.classification == FailureClassification.QUOTA
        assert result.requires_approval is True


class TestVerificationGateContext:
    """Tests for VerificationGateContext."""

    def test_default_context(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
        )
        assert context.task_id == "task-1"
        assert context.attempt_id == "attempt-1"
        assert context.max_replans == 3
        assert context.budget_usd == 100.0
        assert context.max_concurrency == 5
        assert context.required_permissions == set()
        assert context.risk_floor == 0.0

    def test_custom_context(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            max_replans=5,
            budget_usd=50.0,
            max_concurrency=10,
            required_permissions={"deploy", "secrets"},
            risk_floor=0.5,
        )
        assert context.max_replans == 5
        assert context.budget_usd == 50.0
        assert context.max_concurrency == 10
        assert context.required_permissions == {"deploy", "secrets"}
        assert context.risk_floor == 0.5


class TestRunVerificationGates:
    """Tests for run_verification_gates function."""

    def test_all_gates_pass(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            verification_gates=[
                VerificationGate(name="gate1", command="echo success"),
                VerificationGate(name="gate2", command="echo success"),
            ],
        )
        all_passed, results = run_verification_gates(context)

        assert all_passed is True
        assert len(results) == 2
        assert all(r.outcome == VerificationOutcome.PASS for r in results)

    def test_one_gate_fails(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            verification_gates=[
                VerificationGate(name="gate1", command="echo success"),
                VerificationGate(name="gate2", command="exit 1"),
            ],
        )
        all_passed, results = run_verification_gates(context)

        assert all_passed is False
        assert len(results) == 2
        assert results[0].outcome == VerificationOutcome.PASS
        assert results[1].outcome == VerificationOutcome.FAIL

    def test_optional_gate_failure_does_not_block(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            verification_gates=[
                VerificationGate(name="required-gate", required=True, command="exit 1"),
                VerificationGate(name="optional-gate", required=False, command="exit 1"),
            ],
        )
        all_passed, results = run_verification_gates(context)

        assert all_passed is False  # Required gate failed
        assert results[0].outcome == VerificationOutcome.FAIL
        assert results[1].outcome == VerificationOutcome.FAIL

    def test_gate_with_no_command_is_inconclusive(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            verification_gates=[
                VerificationGate(name="no-command-gate"),
            ],
        )
        all_passed, results = run_verification_gates(context)

        assert all_passed is False
        assert results[0].outcome == VerificationOutcome.INCONCLUSIVE
        assert results[0].classification == FailureClassification.PLAN

    def test_evidence_captured(self):
        """Evidence is captured with command output and duration."""
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            verification_gates=[
                VerificationGate(name="evidence-gate", command="sleep 0.01 && echo test output"),
            ],
        )
        all_passed, results = run_verification_gates(context)

        assert all_passed is True
        assert results[0].evidence is not None
        assert results[0].evidence.check_name == "evidence-gate"
        assert results[0].evidence.exit_code == 0
        assert results[0].evidence.duration_ms > 0


class TestCanReplan:
    """Tests for can_replan function."""

    def test_can_replan_under_limit(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            max_replans=3,
            replan_records=[ReplanRecord(), ReplanRecord()],  # 2 replans used
        )
        can, reason = can_replan(context, ReplanReason.VERIFICATION_FAILED)
        assert can is True
        assert reason == ""

    def test_cannot_replan_over_limit(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            max_replans=3,
            replan_records=[ReplanRecord() for _ in range(3)],  # 3 replans used
        )
        can, reason = can_replan(context, ReplanReason.VERIFICATION_FAILED)
        assert can is False
        assert "Max replans (3) exceeded" in reason


class TestProposeReplan:
    """Tests for propose_replan function."""

    def test_valid_replan(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            max_replans=3,
            budget_usd=100.0,
            max_concurrency=5,
            required_permissions={"read"},
            risk_floor=0.1,
            current_plan_hash="abc123",
        )

        new_plan = {"steps": ["step1", "step2", "step3"]}
        record = propose_replan(
            context,
            ReplanReason.VERIFICATION_FAILED,
            new_plan,
        )

        assert record.attempt == 1
        assert record.reason == ReplanReason.VERIFICATION_FAILED
        assert record.original_plan_hash == "abc123"
        assert record.new_plan_hash is not None
        assert record.budget_delta_usd == 0.0
        assert record.concurrency_delta == 0
        assert record.permission_delta == []
        assert record.risk_floor_delta == 0.0

    def test_replan_budget_increase_rejected(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            budget_usd=100.0,
            current_plan_hash="abc123",
        )

        new_plan = {"steps": ["step1"]}
        with pytest.raises(ValueError, match="increase budget"):
            propose_replan(
                context,
                ReplanReason.VERIFICATION_FAILED,
                new_plan,
                budget_usd=150.0,  # Exceeds 100.0
            )

    def test_replan_concurrency_increase_rejected(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            max_concurrency=5,
            current_plan_hash="abc123",
        )

        new_plan = {"steps": ["step1"]}
        with pytest.raises(ValueError, match="increase concurrency"):
            propose_replan(
                context,
                ReplanReason.VERIFICATION_FAILED,
                new_plan,
                max_concurrency=10,  # Exceeds 5
            )

    def test_replan_permission_expansion_rejected(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            required_permissions={"read"},
            current_plan_hash="abc123",
        )

        new_plan = {"steps": ["step1"]}
        with pytest.raises(ValueError, match="add new permissions"):
            propose_replan(
                context,
                ReplanReason.VERIFICATION_FAILED,
                new_plan,
                required_permissions={"read", "write", "deploy"},  # Adds write, deploy
            )

    def test_replan_risk_floor_decrease_rejected(self):
        context = VerificationGateContext(
            task_id="task-1",
            attempt_id="attempt-1",
            risk_floor=0.5,
            current_plan_hash="abc123",
        )

        new_plan = {"steps": ["step1"]}
        with pytest.raises(ValueError, match="decrease risk floor"):
            propose_replan(
                context,
                ReplanReason.VERIFICATION_FAILED,
                new_plan,
                risk_floor=0.1,  # Below 0.5
            )


class TestApproveReplan:
    """Tests for approve_replan function."""

    def test_approve_replan(self):
        record = ReplanRecord(
            replan_id="replan-1",
            attempt=1,
            reason=ReplanReason.VERIFICATION_FAILED,
        )
        assert record.approved is False

        approve_replan(record, "approver-1")

        assert record.approved is True
        assert record.approver == "approver-1"


class TestCompletionEvidence:
    """Tests for completion_evidence function."""

    def test_evidence_bundle(self):
        results = [
            VerificationResult(check_name="gate1", outcome=VerificationOutcome.PASS),
            VerificationResult(check_name="gate2", outcome=VerificationOutcome.FAIL, classification=FailureClassification.TEST),
        ]
        replans = [
            ReplanRecord(replan_id="r1", attempt=1, reason=ReplanReason.VERIFICATION_FAILED),
        ]

        evidence = completion_evidence(
            task_id="task-1",
            attempt_id="attempt-1",
            verification_results=results,
            replan_records=replans,
            plan_hash="abc123",
        )

        assert evidence["task_id"] == "task-1"
        assert evidence["attempt_id"] == "attempt-1"
        assert evidence["plan_hash"] == "abc123"
        assert "completed_at" in evidence
        assert len(evidence["verification_results"]) == 2
        assert evidence["verification_results"][0]["outcome"] == "pass"
        assert evidence["verification_results"][1]["outcome"] == "fail"
        assert evidence["verification_results"][1]["classification"] == "test"
        assert len(evidence["replan_history"]) == 1
        assert evidence["replan_history"][0]["replan_id"] == "r1"
        assert evidence["verification_gates_passed"] is False
        assert evidence["replans_used"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
