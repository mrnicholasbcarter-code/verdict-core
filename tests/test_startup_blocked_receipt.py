"""Runs that end before a plan exists still get a verifiable receipt with their cause."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_a_startup_receipt_cannot_claim_complete(tmp_path: Path) -> None:
    EventLog(tmp_path / "events.jsonl").emit("run_finished", outcome="COMPLETE", reason="claimed")
    assert build_run_receipt(tmp_path)["outcome"] == "BLOCKED"
