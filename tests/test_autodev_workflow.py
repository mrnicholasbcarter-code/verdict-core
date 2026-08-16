"""Tests for the 12-stage autonomous software development workflow (issue #259).

Covers:
- Stage machine structure (12 named stages in order).
- Stage-gate validation: transitions require a gate pass, hook evaluation, and
  evidence receipts; denied gate transitions are blocked.
- Verification stage (10) fails closed when tests fail.
- All-12-stages sequential orchestration run with a mock verifier (no real
  model or network calls).
- Model assignment qualifies passport models and skips quarantined/denied ones.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from verdict.workflows import (
    STAGE_NAMES,
    AutoDevWorkflow,
    ExecutionStrategyKind,
    Stage,
    StageContext,
    StageResult,
    StageStatus,
    Workflow,
    WorkflowGate,
    WorkflowTransitionError,
    WorkflowVerificationError,
    make_evidence_receipt,
)
from verdict.workflows.autodev import _VerificationStage as _RealVerificationStage


def _ok_gate() -> WorkflowGate:
    class _OkGate:
        def evaluate(self, candidates, *, protected=False, dev_mode=False, now=None):
            from verdict.workflows.autodev import _GateResult

            return _GateResult(admitted=list(candidates))

    return _OkGate()


def _blocking_gate() -> WorkflowGate:
    class _BlockingGate:
        def evaluate(self, candidates, *, protected=False, dev_mode=False, now=None):
            from verdict.workflows.autodev import _GateResult

            return _GateResult(admitted=[])

    return _BlockingGate()


def _ok_verifier(returncode: int = 0):
    def verifier(ctx: StageContext, command: list[str]) -> dict:
        return {"returncode": returncode, "stdout": "", "stderr": "stub"}

    return verifier


def _stage(name: str) -> Stage:
    class _Stage(Stage):
        def run(self, ctx: StageContext) -> StageResult:
            return StageResult(
                stage=self.name,
                status=StageStatus.COMPLETED,
                evidence=make_evidence_receipt(self.name, status="completed", detail="ok"),
                artifacts=[{"type": "artifact", "stage": self.name}],
            )

    return _Stage(name)


class _TwoStageWorkflow(Workflow):
    stages: ClassVar[list[Stage]] = [_stage("Alpha"), _stage("Beta")]


class _VerificationFlow(Workflow):
    stages: ClassVar[list[Stage]] = [_stage("Work"), _RealVerificationStage("Verification")]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_stage_names_match_issue_259() -> None:
    assert STAGE_NAMES == (
        "Discovery",
        "Repository Analysis",
        "Memory Lookup",
        "Research",
        "Architecture",
        "Atomic Work Slices",
        "Model Assignment",
        "Delegated Execution",
        "Review",
        "Verification",
        "Learning",
        "Release",
    )


def test_autodev_workflow_exposes_twelve_stages() -> None:
    workflow = AutoDevWorkflow(repo_path=Path("."))
    assert len(workflow.stages) == 12
    assert [stage.name for stage in workflow.stages] == list(STAGE_NAMES)


def test_execution_strategy_kind_has_direct_and_default_is_swarm() -> None:
    assert ExecutionStrategyKind.DIRECT == "direct"
    workflow = AutoDevWorkflow(repo_path=Path("."))
    assert workflow.strategy_kind is ExecutionStrategyKind.SWARM_AUTODEV
    ctx = StageContext("t", Path("."))
    assert ctx.strategy_kind is ExecutionStrategyKind.SWARM_AUTODEV
    direct = AutoDevWorkflow(repo_path=Path("."), strategy_kind=ExecutionStrategyKind.DIRECT)
    assert direct.strategy_kind is ExecutionStrategyKind.DIRECT


def test_evidence_receipt_strategy_ref_and_backward_compat() -> None:
    receipt = make_evidence_receipt("Alpha", status="completed", detail="ok")
    assert receipt["extensions"] == {}
    tagged = make_evidence_receipt(
        "Alpha", status="completed", detail="ok", strategy_ref="direct-frontier/x1"
    )
    assert tagged["extensions"]["strategy_ref"] == "direct-frontier/x1"
    plain = make_evidence_receipt("Alpha", status="completed", detail="ok")
    assert tagged["schema_version"] == plain["schema_version"] == "1"


# ---------------------------------------------------------------------------
# Transition gating
# ---------------------------------------------------------------------------


def test_transition_requires_evidence_receipt() -> None:
    workflow = _TwoStageWorkflow(gate=_ok_gate())
    with pytest.raises(WorkflowTransitionError, match="missing evidence receipt"):
        workflow.validate_transition("Alpha", "Beta", None, candidates=["candidate"])


def test_denied_transition_when_gate_excludes_candidates() -> None:
    workflow = _TwoStageWorkflow(gate=_blocking_gate())
    evidence = make_evidence_receipt("Alpha", status="completed", detail="ok")
    with pytest.raises(WorkflowTransitionError, match="no eligible candidates"):
        workflow.validate_transition("Alpha", "Beta", evidence, candidates=["candidate"])


def test_admitted_transition_requires_hook_evaluation() -> None:
    class _RecordingHook:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def evaluate(self, ctx: StageContext, stage: str) -> None:
            self.calls.append(stage)

    hook = _RecordingHook()
    workflow = _TwoStageWorkflow(gate=_ok_gate(), hook=hook)
    evidence = make_evidence_receipt("Alpha", status="completed", detail="ok")
    workflow.validate_transition(
        "Alpha", "Beta", evidence, candidates=["candidate"], ctx=StageContext("t", Path("."))
    )
    assert hook.calls == ["Beta"]


def test_protected_transition_fails_closed_without_real_gate() -> None:
    workflow = _TwoStageWorkflow()  # uses the fail-closed default gate
    evidence = make_evidence_receipt("Alpha", status="completed", detail="ok")
    with pytest.raises(
        WorkflowTransitionError, match="protected transition requires an eligibility gate"
    ):
        workflow.validate_transition(
            "Alpha", "Beta", evidence, protected=True, candidates=["candidate"]
        )


# ---------------------------------------------------------------------------
# Verification gate
# ---------------------------------------------------------------------------


def test_verification_fails_closed_when_tests_fail() -> None:
    workflow = _VerificationFlow(
        repo_path=Path("."), config={"verifier": _ok_verifier(returncode=1)}
    )
    with pytest.raises(WorkflowVerificationError):
        workflow.run("objective", repo_path=Path("."))


def test_verification_succeeds_when_tests_pass() -> None:
    workflow = _VerificationFlow(
        repo_path=Path("."), config={"verifier": _ok_verifier(returncode=0)}
    )
    result = workflow.run("objective", repo_path=Path("."))
    assert result.status is StageStatus.COMPLETED
    verification = result.stage_results["Verification"]
    assert verification.status is StageStatus.COMPLETED
    assert verification.artifacts[0]["tests"] == "passed"


# ---------------------------------------------------------------------------
# Full orchestration run
# ---------------------------------------------------------------------------


def test_all_twelve_stages_run_sequentially() -> None:
    workflow = AutoDevWorkflow(repo_path=Path("."), config={"verifier": _ok_verifier(returncode=0)})
    result = workflow.run("build an autodev workflow", repo_path=Path("."))
    assert result.status is StageStatus.COMPLETED
    assert list(result.stage_results) == list(STAGE_NAMES)
    completed = {
        name for name, sr in result.stage_results.items() if sr.status is StageStatus.COMPLETED
    }
    assert completed == set(STAGE_NAMES)


def test_run_produces_code_edits_and_unit_tests() -> None:
    workflow = AutoDevWorkflow(repo_path=Path("."), config={"verifier": _ok_verifier(returncode=0)})
    result = workflow.run("ship a feature", repo_path=Path("."))
    kinds = {artifact.get("type") for artifact in result.artifacts}
    assert "work_slice" in kinds
    assert "code_edit" in kinds
    assert "unit_test" in kinds
    assert "release" in kinds


def test_run_produces_evidence_receipts_per_stage() -> None:
    workflow = AutoDevWorkflow(repo_path=Path("."), config={"verifier": _ok_verifier(returncode=0)})
    result = workflow.run("objective", repo_path=Path("."))
    for name in STAGE_NAMES:
        assert result.stage_results[name].evidence, name
        assert result.stage_results[name].evidence["kind"] == "execution"


# ---------------------------------------------------------------------------
# Model assignment
# ---------------------------------------------------------------------------


def test_model_assignment_qualifies_passport_models() -> None:
    from datetime import datetime, timedelta, timezone

    from verdict.model_passports import ModelPassport

    now = datetime.now(timezone.utc)
    passports = [
        ModelPassport(
            provider="a",
            model_id="a/opus",
            auth_state="authorized",
            availability_state="eligible",
            qualified_at=now,
            expires_at=now + timedelta(seconds=300),
            last_verified_timestamp=now,
        ),
        ModelPassport(
            provider="b",
            model_id="b/denied",
            auth_state="unauthorized",
            availability_state="denied",
            qualified_at=now,
            expires_at=now + timedelta(seconds=300),
            last_verified_timestamp=now,
        ),
    ]
    workflow = AutoDevWorkflow(
        repo_path=Path("."), config={"passports": passports, "verifier": _ok_verifier(returncode=0)}
    )
    result = workflow.run("objective", repo_path=Path("."))
    assignments = [a for a in result.artifacts if a.get("type") == "model_assignment"]
    assert assignments
    assert all(a["model"] == "a/opus" for a in assignments)


def test_model_assignment_uses_fallback_without_passports() -> None:
    workflow = AutoDevWorkflow(
        repo_path=Path("."),
        config={"default_model": "auto/default", "verifier": _ok_verifier(returncode=0)},
    )
    result = workflow.run("objective", repo_path=Path("."))
    assignments = [a for a in result.artifacts if a.get("type") == "model_assignment"]
    assert assignments
    assert all(a["model"] == "auto/default" for a in assignments)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_skips_stages_but_validates_transitions() -> None:
    workflow = AutoDevWorkflow(repo_path=Path("."))
    result = workflow.run("objective", repo_path=Path("."), dry_run=True)
    assert result.status is StageStatus.COMPLETED
    assert all(sr.status is StageStatus.SKIPPED for sr in result.stage_results.values())
