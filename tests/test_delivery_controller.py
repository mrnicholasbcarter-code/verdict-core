"""BOD-68 delivery controller — Linear proof-contract unit tests (no live GitHub)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.proof.contract import ProofContract, load_contract
from scripts.proof.runner import CommandResult, ProofRunner
from verdict.delivery import (
    DeliveryBlockedError,
    DeliveryController,
    LocalProofResult,
    RepairAction,
    build_pr_body,
    build_review_packet,
    classify_ci_failure,
    decide_repair_action,
    merge_gates_satisfied,
    post_merge_verify,
    run_local_proof,
)

ROOT = Path(__file__).resolve().parents[1]


def _packet(worktree: Path, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "issue": "BOD-68",
        "issue_url": "https://linear.app/bodanglin/issue/BOD-68",
        "project": "Verdict",
        "team": "Bodanglin",
        "issue_updated_at": "2026-09-19T00:00:00Z",
        "objective": "Automate delivery lifecycle",
        "acceptance_criteria": [
            {"id": "AC1", "text": "Local proof blocks PR", "verification": "unit"},
            {"id": "AC2", "text": "CI classified", "verification": "unit"},
        ],
        "dependencies": [],
        "context": {"refs": ["AGENTS.md"], "omissions": []},
        "spec": "specs/68-delivery/spec.md",
        "plan": "specs/68-delivery/plan.md",
        "tasks": "specs/68-delivery/tasks.md",
        "worktree": str(worktree),
        "branch": "feat/bod-68-autonomous-delivery",
        "base_sha": "a" * 40,
        "ownership": "delivery-controller",
        "constraints": [
            "BOD-104 owns routes",
            "Never force-push main",
            "Fail closed on local proof",
        ],
        "allowed_files": ["verdict/delivery.py", "tests/test_delivery_controller.py"],
        "provider": "omniroute-live",
        "model": "gc/grok-4.6",
        "routing_evidence": "route.json",
        "available_at": "2026-09-19T00:00:00Z",
        "attempt_id": "attempt-68-1",
        "budgets": {"wall_seconds": 3600, "idle_seconds": 600, "ci_seconds": 1200},
        "return_evidence": "receipt.json",
        "lease": {
            "dispatch_id": "attempt-68-1",
            "worker_id": "worker-1",
            "generation": 1,
            "stale_after_seconds": 600,
            "heartbeat_expectation": "30s",
        },
        "proof_requirements": {
            "STATIC": {"required": True, "command": "ruff check"},
            "UNIT": {"required": True, "command": "pytest"},
            "INTEGRATION": {"required": False, "reason": "unit only", "approved_by": "ops"},
            "ACCEPTANCE-PROOF": {"required": True, "command": "proof run"},
        },
    }
    base.update(overrides)
    return base


def _write_fixture_contract(path: Path) -> None:
    payload = {
        "schema_version": 1,
        "name": "bod-68-fixture",
        "runner": "native",
        "modes": {
            "targeted": {"phases": ["lint", "unit_targeted"]},
            "full": {"phases": ["lint", "unit_targeted"]},
        },
        "gates": [
            {"id": "lint", "phase": "lint", "classify": "lint", "command": ["lint-gate"]},
            {
                "id": "unit_targeted",
                "phase": "unit_targeted",
                "classify": "test",
                "command": ["test-gate"],
            },
        ],
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _failing_runner_factory(fail_gate: str = "lint") -> Any:
    def factory(contract: ProofContract, root: Path) -> ProofRunner:
        def executor(argv: list[str], *, env: dict[str, str], cwd: Path) -> CommandResult:
            _ = (env, cwd)
            name = argv[0] if argv else ""
            if name == fail_gate or (fail_gate == "lint" and name == "lint-gate"):
                return CommandResult(exit_code=1, stdout="", stderr=f"{fail_gate} failed")
            if fail_gate == "test" and name == "test-gate":
                return CommandResult(
                    exit_code=1, stdout="tests/test_x.py::test_broken FAILED", stderr=""
                )
            return CommandResult(exit_code=0, stdout="ok", stderr="")

        return ProofRunner(contract, root=root, executor=executor)

    return factory


def _passing_runner_factory() -> Any:
    def factory(contract: ProofContract, root: Path) -> ProofRunner:
        def executor(argv: list[str], *, env: dict[str, str], cwd: Path) -> CommandResult:
            _ = (argv, env, cwd)
            return CommandResult(exit_code=0, stdout="ok", stderr="")

        return ProofRunner(contract, root=root, executor=executor)

    return factory


class _FakeGitHub:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []
        self.merged: list[str] = []

    def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> str:
        self.created.append({"title": title, "body": body, "head": head, "base": base})
        return "https://github.com/example/verdict-core/pull/68"

    def list_required_checks(
        self, *, head_sha: str, pr_url: str | None = None
    ) -> list[dict[str, Any]]:
        _ = (head_sha, pr_url)
        return []

    def get_mergeability(self, *, pr_url: str) -> tuple[str, str]:
        _ = pr_url
        return "MERGEABLE", "CLEAN"

    def merge_pull_request(self, *, pr_url: str, method: str = "squash") -> str:
        self.merged.append(pr_url)
        _ = method
        return "m" * 40

    def list_main_checks(self, *, main_sha: str) -> list[dict[str, Any]]:
        _ = main_sha
        return []

    def list_unresolved_review_threads(self, *, pr_url: str) -> list[str]:
        _ = pr_url
        return []


class _FakeLinear:
    def __init__(self) -> None:
        self.done: list[str] = []
        self.evidence: list[dict[str, Any]] = []

    def record_delivery_evidence(
        self, *, issue: str, pr_url: str, merge_sha: str, evidence: Any
    ) -> None:
        self.evidence.append(
            {"issue": issue, "pr_url": pr_url, "merge_sha": merge_sha, "evidence": dict(evidence)}
        )

    def mark_done(self, *, issue: str) -> None:
        self.done.append(issue)


def test_intentional_local_lint_failure_blocks_pr(tmp_path: Path) -> None:
    contract_path = tmp_path / "contract.yaml"
    _write_fixture_contract(contract_path)
    packet = _packet(tmp_path)
    controller = DeliveryController(runner_factory=_failing_runner_factory("lint"))

    result = controller.run_local_proof(
        packet, mode="targeted", contract_path=contract_path, validate_packet_contract=True
    )
    assert result.ok is False
    assert result.blocks_pr is True
    assert "lint" in result.reason.lower() or any(
        f.get("classify") == "lint" or f.get("gate") == "lint" for f in result.failures
    )

    with pytest.raises(DeliveryBlockedError, match="PR-open blocked"):
        controller.assert_pr_open_allowed(result)

    github = _FakeGitHub()
    controller.github = github
    with pytest.raises(DeliveryBlockedError):
        controller.open_pull_request(
            packet, proof=result, title="BOD-68 should not open", body="nope"
        )
    assert github.created == []


def test_intentional_local_test_failure_blocks_pr(tmp_path: Path) -> None:
    contract_path = tmp_path / "contract.yaml"
    _write_fixture_contract(contract_path)
    packet = _packet(tmp_path)
    result = run_local_proof(
        packet,
        mode="targeted",
        contract_path=contract_path,
        runner_factory=_failing_runner_factory("test"),
    )
    assert result.ok is False
    assert result.blocks_pr is True
    assert result.failures
    assert result.failures[0].get("classify") == "test" or result.failures[0].get("gate") == (
        "unit_targeted"
    )


def test_ci_failure_classification_code_vs_conflict_vs_infra() -> None:
    green = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}]
    code = [
        {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE", "required": True},
        {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True},
    ]
    infra = [
        {
            "name": "runner-availability",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "required": True,
        }
    ]
    assert classify_ci_failure(green) == "GREEN"
    assert classify_ci_failure(code) == "CODE_FAILURE"
    assert classify_ci_failure(infra) == "INFRA_FAILURE"

    code_decision = decide_repair_action(ci_classification="CODE_FAILURE")
    assert code_decision.action == RepairAction.CODE_FIX
    assert code_decision.rewrite_code is True

    conflict = decide_repair_action(merge_classification="CONFLICT")
    assert conflict.action == RepairAction.REBASE
    assert conflict.rewrite_code is False
    assert conflict.force_push_main is False

    infra_decision = decide_repair_action(ci_classification="INFRA_FAILURE")
    assert infra_decision.action == RepairAction.RETRY_INFRA
    assert infra_decision.rewrite_code is False


def test_transient_infra_retries_without_code_rewrite() -> None:
    controller = DeliveryController(max_infra_attempts=2)
    first = controller.decide_repair_action(ci_classification="INFRA_FAILURE")
    assert first.action == RepairAction.RETRY_INFRA
    assert first.rewrite_code is False
    controller.record_repair_outcome(first)

    second = controller.decide_repair_action(ci_classification="INFRA_FAILURE")
    assert second.action == RepairAction.RETRY_INFRA
    assert second.rewrite_code is False
    controller.record_repair_outcome(second)

    third = controller.decide_repair_action(ci_classification="INFRA_FAILURE")
    assert third.action == RepairAction.BLOCK
    assert third.rewrite_code is False


def test_merge_conflict_takes_rebase_path() -> None:
    controller = DeliveryController()
    decision = controller.decide_repair_action(
        ci_classification="CODE_FAILURE",  # ignored when merge conflicts
        mergeable="CONFLICTING",
        merge_state_status="DIRTY",
    )
    assert decision.action == RepairAction.REBASE
    assert decision.lifecycle_hint == "IMPLEMENTING"
    assert decision.force_push_main is False


def test_worker_repairs_then_escalate_then_block() -> None:
    controller = DeliveryController(max_worker_attempts=2)
    d0 = controller.decide_repair_action(ci_classification="CODE_FAILURE")
    assert d0.action == RepairAction.CODE_FIX
    controller.record_repair_outcome(d0)

    d1 = controller.decide_repair_action(ci_classification="CODE_FAILURE")
    assert d1.action == RepairAction.CODE_FIX
    controller.record_repair_outcome(d1)

    d2 = controller.decide_repair_action(ci_classification="CODE_FAILURE")
    assert d2.action == RepairAction.ESCALATE
    controller.record_repair_outcome(d2)

    d3 = controller.decide_repair_action(ci_classification="CODE_FAILURE")
    assert d3.action == RepairAction.BLOCK


def test_post_merge_red_main_blocks_next_transition() -> None:
    red_checks = [
        {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE", "required": True}
    ]
    result = post_merge_verify(main_sha="b" * 40, main_checks=red_checks)
    assert result.ok is False
    assert result.blocks_next_transition is True
    assert result.classification == "CODE_FAILURE"

    controller = DeliveryController(linear=_FakeLinear())
    with pytest.raises(DeliveryBlockedError, match="post-merge"):
        controller.closeout_linear(
            issue="BOD-68", pr_url="https://example/pr/1", merge_sha="c" * 40, post_merge=result
        )
    assert controller.linear.done == []  # type: ignore[union-attr]


def test_post_merge_green_allows_linear_done() -> None:
    green = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}]
    result = post_merge_verify(main_sha="d" * 40, main_checks=green)
    assert result.ok is True
    assert result.blocks_next_transition is False

    linear = _FakeLinear()
    controller = DeliveryController(linear=linear)
    controller.closeout_linear(
        issue="BOD-68", pr_url="https://example/pr/1", merge_sha="e" * 40, post_merge=result
    )
    assert linear.done == ["BOD-68"]
    assert linear.evidence


def test_merge_gates_require_astra_ci_and_threads() -> None:
    ok = merge_gates_satisfied(
        acceptance_complete=True,
        local_proof_ok=True,
        astra_approved=True,
        ci_classification="GREEN",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
    )
    assert ok.ok is True

    blocked = merge_gates_satisfied(
        acceptance_complete=True,
        local_proof_ok=True,
        astra_approved=False,
        ci_classification="GREEN",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        unresolved_review_threads=["thread-1"],
    )
    assert blocked.ok is False
    assert "astra_approval_missing" in blocked.blockers
    assert any(b.startswith("unresolved_review_threads") for b in blocked.blockers)


def test_passing_local_proof_allows_pr_open(tmp_path: Path) -> None:
    contract_path = tmp_path / "contract.yaml"
    _write_fixture_contract(contract_path)
    packet = _packet(tmp_path)
    github = _FakeGitHub()
    controller = DeliveryController(github=github, runner_factory=_passing_runner_factory())
    proof = controller.run_local_proof(packet, mode="targeted", contract_path=contract_path)
    assert proof.ok is True
    assert proof.blocks_pr is False

    url = controller.open_pull_request(
        packet,
        proof=proof,
        title="[BOD-68] Automate local proof, Astra review, PR creation, CI repair and merge",
        body=build_pr_body(packet, proof_result=proof),
    )
    assert "pull/68" in url
    assert len(github.created) == 1
    assert "BOD-68" in github.created[0]["body"]


def test_review_packet_is_compact_not_full_trajectory(tmp_path: Path) -> None:
    packet = _packet(tmp_path)
    proof = LocalProofResult(
        ok=True,
        mode="full",
        head_sha="f" * 40,
        report={"ok": True},
        failures=(),
        blocks_pr=False,
        reason="pass",
    )
    review = build_review_packet(
        packet,
        proof_result=proof,
        diff_summary="verdict/delivery.py (+400)\ntests/test_delivery_controller.py (+200)",
        evidence_refs=["evidence/unit.log"],
    )
    payload = review.to_dict()
    assert payload["issue"] == "BOD-68"
    assert "Never force-push main" in payload["architectural_constraints"]
    assert "trajectory" not in payload
    assert "messages" not in payload
    assert payload["diff_summary"]
    assert payload["proof_contract_summary"]["local_proof_ok"] is True


def test_checked_in_proof_contract_still_loads() -> None:
    contract = load_contract(ROOT / "proof" / "contract.yaml")
    assert contract.runner == "native"
