"""Tests for verdict.orchestration.deliver (the pure delivery eligibility gate)."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration import deliver
from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.deliver import (
    DeliverPlan,
    DeliverReason,
    DeliverRefusal,
    DeliverRequest,
    head_branch_name,
    plan_delivery,
    slugify_title,
)
from verdict.orchestration.receipt import EventLog, verify_run_receipt, write_run_receipt

VERIFY = ("pytest", "-q", "tests/test_a.py")
INTEGRATION = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
PASS = {"status": "PASS", "reviewer": "ocr v1", "route_id": "gm/gemini-3", "findings": []}


def _graph() -> WorkGraph:
    return WorkGraph(
        goal="add feature x",
        nodes=(WorkNode("a", "build a", owned_files=("pkg/a.py",), verification_command=VERIFY),),
    )


def _make_run(
    tmp_path: Path,
    *,
    outcome: str = "COMPLETE",
    review: dict[str, Any] | None = PASS,
    finished: bool = True,
    barrier_ok: bool = True,
    level: str | None = "family",
    no_change: bool = False,
    integration: str = INTEGRATION,
    receipt: bool = True,
) -> Path:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    graph = _graph().to_dict()
    (run_dir / "graph.json").write_text(json.dumps(graph))
    log = EventLog(run_dir / "events.jsonl")
    log.emit("run_started", run_id="run-1", goal="add feature x")
    log.emit("dispatch", node_id="a", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="x")
    log.emit("terminal", node_id="a", attempt=1, ok=True, duration_seconds=2.0)
    log.emit("verify", node_id="a", ok=True, command=list(VERIFY), exit_code=0)
    if no_change:
        log.emit("barrier", node_id="a", name="no_change", ok=True)
    log.emit("integrate", ok=True, commits=[integration], ref=integration, commit=integration)
    log.emit("barrier", name="integration", ok=barrier_ok)
    if level is not None:
        log.emit("controller", state="REVIEW_INDEPENDENCE", level=level, reviewer_route="x")
    if review is not None:
        (run_dir / "review.json").write_text(json.dumps(review))
    if finished:
        log.emit("run_finished", outcome=outcome, reason="done", integration_ref=integration)
    if receipt:
        write_run_receipt(run_dir)
    return run_dir


def _request(run_dir: Path, **kw: Any) -> DeliverRequest:
    return DeliverRequest(run_dir=run_dir, issue_number=1269, issue_title="Deliver it!", **kw)


def _refused(decision: object) -> DeliverRefusal:
    assert isinstance(decision, DeliverRefusal), decision
    assert decision.message
    return decision


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("deliver gate must not spawn subprocesses")

    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, boom)
    monkeypatch.setattr("os.system", boom)


def test_complete_verified_run_yields_plan(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    assert verify_run_receipt(run_dir, repair=False) == []
    plan = plan_delivery(_request(run_dir))
    assert isinstance(plan, DeliverPlan)
    assert plan.run_id == "run-1"
    assert plan.goal == "add feature x"
    assert plan.issue_number == 1269
    assert plan.base_branch == "main"
    assert plan.head_branch == "verdict/issue-1269-deliverit"
    assert plan.integration_commit == INTEGRATION
    assert plan.receipt_path == run_dir / "receipt.json"
    assert plan.receipt_digest.startswith("sha256:")
    assert plan.events_digest.startswith("sha256:")
    assert [n.node_id for n in plan.nodes] == ["a"]
    assert plan.nodes[0].final_state == "VALIDATED"
    assert plan.nodes[0].routes == ("cc/claude-sonnet-5",)
    assert plan.nodes[0].verification[0].exit_code == 0
    assert plan.review.status == "PASS"
    assert plan.review.level == "family"
    assert len(plan.review.implementer_families) == 1
    assert plan.review.reviewer_family
    assert plan.review.reviewer_family not in plan.review.implementer_families


def test_plan_is_deterministic_and_does_not_write(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    before = {p.name: p.read_bytes() for p in run_dir.iterdir()}
    first = plan_delivery(_request(run_dir, base_branch="develop"))
    second = plan_delivery(_request(run_dir, base_branch="develop"))
    assert first == second
    assert isinstance(first, DeliverPlan)
    assert first.base_branch == "develop"
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == before


def test_plan_records_route_level_review_and_no_openspec_change(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, level="route")
    plan = plan_delivery(_request(run_dir))
    assert isinstance(plan, DeliverPlan)
    assert plan.review.level == "route"
    assert plan.openspec_change is None


def test_run_not_found(tmp_path: Path) -> None:
    refusal = _refused(plan_delivery(_request(tmp_path / "nope")))
    assert refusal.code is DeliverReason.RUN_NOT_FOUND
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _refused(plan_delivery(_request(empty))).code is DeliverReason.RUN_NOT_FOUND


def test_in_progress_run_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, finished=False, receipt=False)
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RUN_IN_PROGRESS


def test_blocked_run_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, outcome="BLOCKED")
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RUN_NOT_COMPLETE


def test_cancelled_run_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, outcome="CANCELLED")
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RUN_NOT_COMPLETE


def test_claimed_complete_but_receipt_blocked_is_not_delivered(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, barrier_ok=False)
    refusal = _refused(plan_delivery(_request(run_dir)))
    assert refusal.code is DeliverReason.RUN_NOT_COMPLETE


def test_missing_receipt_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, receipt=False)
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RECEIPT_MISSING


def test_tampered_events_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    log = EventLog(run_dir / "events.jsonl")
    log.emit("controller", action="late edit")
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RECEIPT_INVALID


def test_corrupt_receipt_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    (run_dir / "receipt.json").write_text("{not json")
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RECEIPT_INVALID


def test_edited_receipt_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    receipt["goal"] = "something else"
    (run_dir / "receipt.json").write_text(json.dumps(receipt))
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RECEIPT_INVALID


def test_corrupt_events_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    with (run_dir / "events.jsonl").open("a") as stream:
        stream.write("garbage\n")
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.RECEIPT_INVALID


@pytest.mark.parametrize(
    "review",
    [
        None,
        {"status": "FAIL", "reviewer": "ocr", "route_id": "gm/g", "findings": []},
        {"status": "SKIPPED", "reviewer": "ocr", "route_id": "gm/g", "findings": []},
        {
            "status": "PASS",
            "reviewer": "ocr",
            "route_id": "gm/g",
            "findings": [{"severity": "high"}],
        },
    ],
)
def test_failed_or_missing_review_refused(tmp_path: Path, review: dict[str, Any] | None) -> None:
    run_dir = _make_run(tmp_path, review=review)
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.REVIEW_FAILED


def test_run_with_no_changes_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, no_change=True)
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.NOTHING_TO_DELIVER


def test_run_without_integration_commit_refused(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, integration="")
    assert _refused(plan_delivery(_request(run_dir))).code is DeliverReason.NOTHING_TO_DELIVER


@pytest.mark.parametrize("issue", [0, -3])
def test_invalid_issue_number_refused(tmp_path: Path, issue: int) -> None:
    run_dir = _make_run(tmp_path)
    request = DeliverRequest(run_dir=run_dir, issue_number=issue)
    assert _refused(plan_delivery(request)).code is DeliverReason.INVALID_REQUEST


def test_reason_codes_are_distinct_and_stable() -> None:
    values = [r.value for r in DeliverReason]
    assert len(values) == len(set(values))
    assert {
        "run_not_found",
        "run_not_complete",
        "receipt_missing",
        "receipt_invalid",
        "review_failed",
        "nothing_to_deliver",
    } <= set(values)


def test_slug_is_sanitised_and_bounded() -> None:
    assert slugify_title("Fix: the `rm -rf /` bug; $(whoami)!") == "fixtherm-rfbugwhoami"
    assert len(slugify_title("a" * 100)) == 40
    assert slugify_title("日本語") == ""
    assert head_branch_name(7, "") == "verdict/issue-7"
    assert head_branch_name(7, "Hello World") == "verdict/issue-7-helloworld"
    assert "/" not in slugify_title("a/b")


def test_contracts_are_frozen_dataclasses(tmp_path: Path) -> None:
    for cls in (
        deliver.DeliverRequest,
        deliver.DeliverPlan,
        deliver.DeliverRefusal,
        deliver.NodeSummary,
        deliver.ReviewEvidence,
        deliver.VerificationResult,
    ):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
    refusal = DeliverRefusal(code=DeliverReason.RUN_NOT_FOUND, message="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        refusal.message = "x"  # type: ignore[misc]


def test_module_does_not_import_process_or_network_modules() -> None:
    source = Path(deliver.__file__).read_text()
    for banned in ("import subprocess", "import socket", "import urllib", "import requests"):
        assert banned not in source
