"""Tests for verdict.orchestration.receipt (run receipt / evidence chain)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import OrchestrationError, WorkGraph, WorkNode
from verdict.orchestration.receipt import (
    RECEIPT_SCHEMA,
    EventLog,
    build_run_receipt,
    completion_verdict,
    verify_run_receipt,
    write_run_receipt,
)

FIXED = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
VERIFY_A = ("pytest", "-q", "tests/test_a.py")


def _clock() -> datetime:
    return FIXED


def _graph() -> WorkGraph:
    return WorkGraph(
        goal="add feature x",
        nodes=(
            WorkNode(
                "a",
                "build a",
                owned_files=("pkg/a.py",),
                verification_command=VERIFY_A,
                barrier="int",
            ),
            WorkNode(
                "b",
                "build b",
                owned_files=("pkg/b.py",),
                verification_command=("pytest", "-q", "tests/test_b.py"),
                barrier="int",
            ),
            WorkNode("merge", "integrate a+b", kind="integrate", depends_on=("a", "b")),
            WorkNode("rev", "review", kind="review", depends_on=("merge",)),
        ),
    )


def _run(
    tmp_path: Path, *, review: dict[str, Any] | None = None, skip: str = "", barrier_ok: bool = True
) -> Path:
    """A run where node a fails once (fault injected), is reassigned, then validates."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("run_started", run_id="run-1", goal="add feature x")
    log.emit(
        "dispatch",
        node_id="a",
        attempt=1,
        route_id="cc/claude-sonnet-5",
        capacity_class="subscription",
        fault_injected=True,
    )
    log.emit(
        "terminal", node_id="a", attempt=1, ok=False, duration_seconds=1.5, fault_injected=True
    )
    log.emit("failure", node_id="a", attempt=1, category="rate_limited")
    log.emit("cooldown", node_id="a", route_id="cc/claude-sonnet-5", seconds=60)
    log.emit("reassign", node_id="a", from_route="cc/claude-sonnet-5", to_route="gh/gpt-5")
    log.emit("dispatch", node_id="a", attempt=2, route_id="gh/gpt-5", capacity_class="free")
    log.emit("terminal", node_id="a", attempt=2, ok=True, duration_seconds=4.0)
    if skip != "verify_a":
        log.emit("verify", node_id="a", ok=True, command=list(VERIFY_A), exit_code=0)
    log.emit("dispatch", node_id="b", attempt=1, route_id="gh/gpt-5", capacity_class="free")
    log.emit("terminal", node_id="b", attempt=1, ok=True, duration_seconds=2.0)
    log.emit("verify", node_id="b", ok=True, command=["pytest"], exit_code=0)
    log.emit("barrier", node_id="merge", name="int", ok=barrier_ok)
    log.emit("integrate", node_id="merge", commit="abc123")
    log.emit("terminal", node_id="merge", attempt=1, ok=True, route_id="local/git")
    log.emit("verify", node_id="merge", ok=True, command=["pytest"], exit_code=0)
    log.emit("controller", action="budget_check", remaining=3)
    if review is not None:
        (run_dir / "review.json").write_text(json.dumps(review))
    log.emit("run_finished", outcome="COMPLETE", reason="done")
    return run_dir


PASS = {"status": "PASS", "reviewer": "ocr v1", "route_id": "gm/gemini-3", "findings": []}


# ---------------------------------------------------------------- EventLog


def test_seq_continues_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, clock=_clock)
    assert [log.emit("heartbeat").seq for _ in range(3)] == [1, 2, 3]
    reopened = EventLog(path, clock=_clock)
    assert reopened.last_seq == 3
    assert reopened.emit("heartbeat", node_id="a").seq == 4
    assert [e.seq for e in reopened.read()] == [1, 2, 3, 4]


def test_reopen_drops_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    EventLog(path, clock=_clock).emit("heartbeat")
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"seq": 2, "at": "x", "ty')
    log = EventLog(path, clock=_clock)
    assert log.emit("heartbeat").seq == 2
    assert [e.seq for e in log.read()] == [1, 2]


def test_concurrent_emit_is_strictly_monotonic(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl", clock=_clock)
    threads = [
        threading.Thread(target=lambda: [log.emit("heartbeat") for _ in range(25)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [e.seq for e in log.read()] == list(range(1, 101))


def test_secrets_are_scrubbed_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, clock=_clock)
    fake_key = "sk-" + "A1b2C3d4E5f6G7h8"
    event = log.emit(
        "failure",
        node_id="a",
        error=f"auth {fake_key} failed",
        headers={"Authorization": "Bearer " + "tok_abcdefghij"},
        url=["https://x/?api_key=" + "zzzz9999", "keep me"],
    )
    raw = path.read_text()
    for secret in (fake_key, "tok_abcdefghij", "zzzz9999"):
        assert secret not in raw
    assert "[REDACTED]" in event.data["error"]
    assert event.data["url"][1] == "keep me"
    assert event.data["headers"]["Authorization"] == "Bearer [REDACTED]"


@pytest.mark.parametrize("bad", [object(), {1, 2}, float("nan"), {1: "int key"}, b"bytes"])
def test_non_serializable_data_is_rejected(tmp_path: Path, bad: Any) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, clock=_clock)
    with pytest.raises(OrchestrationError):
        log.emit("heartbeat", value=bad)
    assert not path.exists() or path.read_text() == ""
    assert log.emit("heartbeat").seq == 1


def test_unknown_event_type_rejected(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl", clock=_clock)
    with pytest.raises(OrchestrationError):
        log.emit("not_a_type")
    assert log.last_seq == 0


# ---------------------------------------------------------------- receipt


def test_receipt_fields_with_reassignment_and_fault_injection(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review=PASS))
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["run_id"] == "run-1"
    assert receipt["goal"] == "add feature x"
    assert receipt["graph_digest"] == _graph().digest()
    assert receipt["topology"] == "PARALLEL_WORK_UNITS"
    assert receipt["event_count"] == 18
    assert receipt["events_digest"].startswith("sha256:")
    assert receipt["started_at"] == receipt["finished_at"] == "2025-01-02T03:04:05Z"
    nodes = {n["node_id"]: n for n in receipt["nodes"]}
    a = nodes["a"]
    assert a["final_state"] == "VALIDATED"
    first, second = a["attempts"]
    assert first == {
        "attempt": 1,
        "route_id": "cc/claude-sonnet-5",
        "provider": "cc",
        "capacity_class": "subscription",
        "outcome": "failure",
        "failure_category": "rate_limited",
        "duration_seconds": 1.5,
        "fault_injected": True,
    }
    assert second["route_id"] == "gh/gpt-5" and second["outcome"] == "success"
    assert second["fault_injected"] is False and second["capacity_class"] == "free"
    assert a["validated_by"][0]["command"] == list(VERIFY_A)
    assert a["validated_by"][0]["exit_code"] == 0
    assert nodes["merge"]["commit"] == "abc123"
    assert nodes["rev"]["final_state"] == "PLANNED"
    assert receipt["reassignments"][0]["to_route"] == "gh/gpt-5"
    assert receipt["cooldowns"][0]["seconds"] == 60
    assert receipt["controller_events"][0]["action"] == "budget_check"
    assert receipt["review"] == {
        "status": "PASS",
        "reviewer": "ocr v1",
        "route_id": "gm/gemini-3",
        "blocking": 0,
    }
    assert (receipt["outcome"], receipt["reason"].split(",")[0]) == (
        "COMPLETE",
        "all implement/integrate nodes validated",
    )


def test_verify_before_last_success_does_not_validate(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS)
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("dispatch", node_id="b", attempt=2, route_id="gh/gpt-5")
    log.emit("terminal", node_id="b", attempt=2, ok=True)
    nodes = {n["node_id"]: n for n in build_run_receipt(run_dir)["nodes"]}
    assert nodes["b"]["final_state"] == "TERMINAL_SUCCESS"
    assert nodes["b"]["validated_by"] == []


def test_failed_verify_does_not_validate(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS, skip="verify_a")
    EventLog(run_dir / "events.jsonl").emit("verify", node_id="a", ok=False, exit_code=1)
    receipt = build_run_receipt(run_dir)
    assert receipt["nodes"][0]["final_state"] == "TERMINAL_SUCCESS"
    assert receipt["outcome"] == "BLOCKED"


def test_write_and_verify_receipt_roundtrip(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS)
    path = write_run_receipt(run_dir)
    assert path == run_dir / "receipt.json"
    assert json.loads(path.read_text())["schema"] == RECEIPT_SCHEMA
    assert verify_run_receipt(run_dir) == []
    assert not list(run_dir.glob(".receipt-*"))


def test_tampered_events_detected(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS)
    write_run_receipt(run_dir)
    events = run_dir / "events.jsonl"
    events.write_text(events.read_text().replace('"ok":false', '"ok":true'))
    problems = verify_run_receipt(run_dir)
    assert any("events_digest" in p for p in problems)


def test_appended_event_and_edited_receipt_detected(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS)
    write_run_receipt(run_dir)
    EventLog(run_dir / "events.jsonl").emit("heartbeat")
    assert any("events_digest" in p for p in verify_run_receipt(run_dir))
    write_run_receipt(run_dir)
    receipt_path = run_dir / "receipt.json"
    stored = json.loads(receipt_path.read_text())
    stored["outcome"] = "BLOCKED"
    receipt_path.write_text(json.dumps(stored))
    assert verify_run_receipt(run_dir) == [
        "outcome mismatch: stored receipt differs from recomputed evidence"
    ]


def test_verify_missing_receipt(tmp_path: Path) -> None:
    assert verify_run_receipt(tmp_path) == ["missing receipt.json"]


# ---------------------------------------------------------------- completion verdict


def test_completion_complete(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review=PASS))
    assert completion_verdict(receipt)[0] == "COMPLETE"


def test_blocked_when_node_not_validated(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review=PASS, skip="verify_a"))
    status, reason = completion_verdict(receipt)
    assert status == "BLOCKED"
    assert reason == "node a not VALIDATED (final_state=TERMINAL_SUCCESS)"


def test_blocked_when_barrier_failed(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review=PASS, barrier_ok=False))
    assert completion_verdict(receipt) == ("BLOCKED", "integration barrier failed: int")


def test_blocked_when_barrier_missing(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review=PASS))
    receipt["integration"] = {"ok": False, "barriers": [], "missing": ["int"]}
    assert completion_verdict(receipt) == ("BLOCKED", "integration barrier missing: int")


def test_blocked_when_review_missing(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path))
    assert receipt["review"]["status"] == "MISSING"
    assert completion_verdict(receipt) == ("BLOCKED", "review status MISSING (PASS required)")


def test_blocked_when_review_failed(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review={**PASS, "status": "FAIL"}))
    assert completion_verdict(receipt) == ("BLOCKED", "review status FAIL (PASS required)")


def test_blocked_when_review_pass_has_blocking_finding(tmp_path: Path) -> None:
    finding = {
        "severity": "high",
        "category": "bug",
        "file": "pkg/a.py",
        "line": 3,
        "message": "off by one",
    }
    receipt = build_run_receipt(_run(tmp_path, review={**PASS, "findings": [finding]}))
    assert receipt["review"]["blocking"] == 1
    assert completion_verdict(receipt) == ("BLOCKED", "review has 1 blocking finding(s)")


def test_blocked_when_no_work_nodes_or_bad_schema() -> None:
    assert completion_verdict({"schema": RECEIPT_SCHEMA, "nodes": []})[0] == "BLOCKED"
    assert completion_verdict({"schema": "other"})[1].startswith("unknown receipt schema")


def test_review_from_event_when_no_review_file(tmp_path: Path) -> None:
    run_dir = _run(tmp_path)
    EventLog(run_dir / "events.jsonl").emit(
        "review", node_id="rev", status="PASS", reviewer="ocr", route_id="gm/gemini-3", blocking=0
    )
    receipt = build_run_receipt(run_dir)
    assert receipt["review"]["status"] == "PASS"
    assert receipt["outcome"] == "COMPLETE"
