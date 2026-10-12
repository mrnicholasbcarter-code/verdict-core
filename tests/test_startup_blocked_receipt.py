"""Runs that end before a plan exists still get a verifiable receipt with their cause."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.orchestration.contracts import OrchestrationError
from verdict.orchestration.receipt import (
    EventLog,
    build_run_receipt,
    verify_run_receipt,
    write_run_receipt,
)


def _blocked(run_dir: Path, reason: str) -> None:
    EventLog(run_dir / "events.jsonl").emit(
        "run_finished", outcome="BLOCKED", reason=reason, phase="startup"
    )


def test_startup_blocked_run_without_graph_gets_a_verifiable_receipt(tmp_path: Path) -> None:
    _blocked(tmp_path, "gateway discovery failed: 401 unauthorized")
    write_run_receipt(tmp_path)
    assert verify_run_receipt(tmp_path) == []
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert (receipt["outcome"], receipt["reason"]) == (
        "BLOCKED",
        "gateway discovery failed: 401 unauthorized",
    )
    assert receipt["nodes"] == [] and receipt["graph_digest"] is None


def test_planning_blocked_run_with_empty_graph_keeps_its_cause(tmp_path: Path) -> None:
    (tmp_path / "graph.json").write_text(json.dumps({"goal": "g", "nodes": [], "run_id": "r"}))
    _blocked(tmp_path, "planner returned no valid graph")
    receipt = build_run_receipt(tmp_path)
    assert receipt["reason"] == "planner returned no valid graph"
    assert receipt["goal"] == "g"


def test_a_planned_run_whose_graph_is_missing_stays_unverifiable(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.emit("plan_ready", nodes=1, layers=1, topology="single")
    log.emit("node_state", node_id="a", state="VALIDATED")
    log.emit("run_finished", outcome="COMPLETE", reason="all nodes validated")
    with pytest.raises(OrchestrationError):
        build_run_receipt(tmp_path)


def test_a_completed_claim_without_a_plan_is_not_a_pre_plan_receipt(tmp_path: Path) -> None:
    EventLog(tmp_path / "events.jsonl").emit("run_finished", outcome="COMPLETE", reason="x")
    with pytest.raises(OrchestrationError):
        build_run_receipt(tmp_path)


def test_pre_plan_reason_is_redacted(tmp_path: Path) -> None:
    _blocked(tmp_path, "gateway failed: Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwx")
    receipt = build_run_receipt(tmp_path)
    assert "sk-live-abcdefghijklmnopqrstuvwx" not in json.dumps(receipt)
