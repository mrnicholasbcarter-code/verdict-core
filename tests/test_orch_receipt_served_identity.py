"""Run receipts bind the OmniRoute-served identity per attempt."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.receipt import (
    EventLog,
    build_run_receipt,
    verify_run_receipt,
    write_run_receipt,
)
from verdict.served_identity import check_served, served_identity_from_headers

FIXED = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ROUTE = "claude/claude-sonnet-5"


def _clock() -> datetime:
    return FIXED


def _headers(provider: str, model: str, connection: str = "conn-1") -> dict[str, str]:
    return {
        "X-OmniRoute-Provider": provider,
        "X-OmniRoute-Model": model,
        "X-OmniRoute-Decision": f"strategy=single; provider={provider}; latency_ms=12",
        "X-OmniRoute-Selected-Connection-Id": connection,
    }


def _run(tmp_path: Path, **terminal: Any) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    node = WorkNode(
        "a", "build a", owned_files=("pkg/a.py",), verification_command=("pytest", "-q")
    )
    graph = WorkGraph(goal="g", nodes=(node,))
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("run_started", run_id="run-1", goal="g")
    log.emit("dispatch", node_id="a", attempt=1, route_id=ROUTE, capacity_class="subscription")
    log.emit("terminal", node_id="a", attempt=1, route_id=ROUTE, **terminal)
    log.emit("run_finished", outcome="COMPLETE", reason="done")
    return run_dir


def _attempt(receipt: dict[str, Any]) -> dict[str, Any]:
    attempt: dict[str, Any] = receipt["nodes"][0]["attempts"][0]
    return attempt


def test_verified_served_identity_is_a_match_with_connection(tmp_path: Path) -> None:
    check = check_served(ROUTE, served_identity_from_headers(_headers("cc", "claude-sonnet-5")))
    receipt = build_run_receipt(
        _run(tmp_path, ok=True, reported_model="claude-sonnet-5", served_check=check.to_dict())
    )
    attempt = _attempt(receipt)
    assert attempt["route_identity"] == "match"
    assert attempt["executed_model"] == "cc/claude-sonnet-5"
    assert attempt["served_identity"] == {
        "status": "verified",
        "served_route": "cc/claude-sonnet-5",
        "provider": "cc",
        "model": "claude-sonnet-5",
        "connection_id": "conn-1",
        "decision_strategy": "single",
        "reasons": [],
    }
    assert "route_identity_warning" not in receipt


def test_successful_served_mismatch_warns(tmp_path: Path) -> None:
    check = check_served(ROUTE, served_identity_from_headers(_headers("gh", "gpt-5")))
    receipt = build_run_receipt(
        _run(tmp_path, ok=True, reported_model=ROUTE, served_check=check.to_dict())
    )
    attempt = _attempt(receipt)
    assert attempt["route_identity"] == "mismatch"
    assert attempt["executed_model"] == "gh/gpt-5"
    assert attempt["served_identity"]["reasons"] == ["provider_differs", "model_differs"]
    assert receipt["route_identity_summary"]["mismatch"] == 1
    assert "route_identity_warning" in receipt


def test_unknown_served_identity_is_never_a_match(tmp_path: Path) -> None:
    check = check_served(ROUTE, None)
    receipt = build_run_receipt(
        _run(tmp_path, ok=True, reported_model=ROUTE, served_check=check.to_dict())
    )
    attempt = _attempt(receipt)
    assert attempt["route_identity"] == "unattested"
    assert attempt["executed_model"] == ROUTE
    assert attempt["served_identity"]["status"] == "unknown"
    assert attempt["served_identity"]["connection_id"] is None
    assert receipt["route_identity_summary"]["match"] == 0


def test_opaque_selection_is_unattested(tmp_path: Path) -> None:
    check = check_served("auto/best", served_identity_from_headers(_headers("cc", "x")))
    receipt = build_run_receipt(_run(tmp_path, ok=True, served_check=check.to_dict()))
    assert _attempt(receipt)["route_identity"] == "unattested"


def test_failed_model_mismatch_stays_mismatch(tmp_path: Path) -> None:
    check = check_served(ROUTE, served_identity_from_headers(_headers("cc", "claude-sonnet-5")))
    receipt = build_run_receipt(
        _run(tmp_path, ok=False, error="model_mismatch", served_check=check.to_dict())
    )
    assert _attempt(receipt)["route_identity"] == "mismatch"


def test_legacy_terminal_without_served_check_is_unchanged(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, ok=True, reported_model=ROUTE))
    attempt = _attempt(receipt)
    assert attempt["route_identity"] == "match"
    assert attempt["executed_model"] == ROUTE
    assert "served_identity" not in attempt
    assert not any(key.startswith("_terminal") for key in attempt)


def test_receipt_with_served_check_verifies(tmp_path: Path) -> None:
    check = check_served(ROUTE, served_identity_from_headers(_headers("cc", "claude-sonnet-5")))
    run_dir = _run(tmp_path, ok=True, served_check=check.to_dict())
    write_run_receipt(run_dir)
    assert verify_run_receipt(run_dir) == []
    events = run_dir / "events.jsonl"
    events.write_text(events.read_text().replace('"status":"verified"', '"status":"mismatch"'))
    assert verify_run_receipt(run_dir) != []
