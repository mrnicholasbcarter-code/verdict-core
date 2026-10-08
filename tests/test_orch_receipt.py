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
    capture_producer,
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


def test_receipt_persists_run_retry_budget(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit(
        "run_started",
        run_id="run-1",
        goal="add feature x",
        retry_budget={"max_attempts_per_node": 4, "max_parallel": 2},
    )
    receipt = build_run_receipt(run_dir)
    assert receipt["retry_budget"] == {"max_attempts_per_node": 4, "max_parallel": 2}


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
        "intended_route": "cc/claude-sonnet-5",
        "executed_model": None,
        "route_identity": "unattested",
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
    assert "no_change_nodes" not in json.loads(path.read_text())


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


def test_attempt_without_terminal_from_dead_controller_is_abandoned(tmp_path: Path) -> None:
    from verdict.orchestration.receipt import build_run_receipt

    run_dir = _run(tmp_path, review=PASS)
    log = EventLog(run_dir / "events.jsonl")
    # A later controller life re-dispatches node a after a stalled attempt 3.
    log.emit("dispatch", node_id="a", attempt=3, route_id="cc/hung")
    log.emit("dispatch", node_id="a", attempt=4, route_id="cx/ok")
    log.emit("terminal", node_id="a", attempt=4, ok=True, route_id="cx/ok")
    log.emit("verify", node_id="a", ok=True, command=["pytest"], exit_code=0)
    receipt = build_run_receipt(run_dir)
    node_a = next(n for n in receipt["nodes"] if n["node_id"] == "a")
    outcomes = {a["attempt"]: a["outcome"] for a in node_a["attempts"]}
    assert outcomes[3] == "abandoned" and outcomes[4] == "success"


def test_receipt_review_lists_every_reviewer_attempt(tmp_path: Path) -> None:
    """BOD-224: the receipt shows all reviewer attempts, not only the final one."""
    from verdict.orchestration.receipt import build_run_receipt

    run_dir = _run(tmp_path, review=PASS)
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit(
        "review_attempt",
        attempt=1,
        route_id="cc/stuck",
        status="ERROR",
        category="timeout",
        scope="route",
        cooldown_seconds=120,
        duration_seconds=300.0,
        detail="ocr review timed out: no progress for 300s",
    )
    log.emit("review_attempt", attempt=2, route_id="gm/gemini-3", status="PASS", category="")
    review = build_run_receipt(run_dir)["review"]
    assert review["status"] == "PASS" and review["route_id"] == "gm/gemini-3"
    assert [a["route_id"] for a in review["attempts"]] == ["cc/stuck", "gm/gemini-3"]
    assert review["attempts"][0]["category"] == "timeout"
    assert review["attempts"][0]["duration_seconds"] == 300.0


def test_receipt_review_without_attempts_is_unchanged(tmp_path: Path) -> None:
    """Receipts from runs with no review_attempt events keep their exact shape."""
    from verdict.orchestration.receipt import build_run_receipt

    review = build_run_receipt(_run(tmp_path, review=PASS))["review"]
    assert "attempts" not in review


# ---- BOD-90: outcome-records sidecar write is guarded ----


def test_sidecar_oserror_does_not_crash_receipt(tmp_path: Path) -> None:
    """OSError writing outcome-records.jsonl must not prevent receipt creation."""
    run_dir = _run(tmp_path, review=PASS)
    # Place a directory at the sidecar path so the write raises OSError
    sidecar = run_dir / "outcome-records.jsonl"
    sidecar.mkdir()

    import io
    import sys

    captured = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured
    try:
        path = write_run_receipt(run_dir)
    finally:
        sys.stderr = old_stderr

    # Receipt was written and verifies cleanly
    assert path.exists()
    assert json.loads(path.read_text())["schema"] == RECEIPT_SCHEMA
    assert verify_run_receipt(run_dir) == []

    # Warning was emitted to stderr
    warning = captured.getvalue()
    assert "outcome-records.jsonl" in warning
    assert "warning" in warning.lower()

    # No outcome-records.jsonl file (the directory is still there)
    assert sidecar.is_dir()


def test_sidecar_builder_bug_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug in build_outcome_records (non-OSError) must still propagate."""
    import verdict.outcome_records as orm

    run_dir = _run(tmp_path, review=PASS)

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise ValueError("bug in record builder")

    monkeypatch.setattr(orm, "build_outcome_records", _boom)

    with pytest.raises(ValueError, match="bug in record builder"):
        write_run_receipt(run_dir)


def test_no_change_nodes_listed_on_receipt(tmp_path: Path) -> None:
    """Validated implement nodes with an ok no_change barrier appear in the summary."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="already there",
        nodes=(
            WorkNode(
                "parser",
                "keep parser",
                owned_files=("pkg/parser.py",),
                verification_command=("true",),
            ),
            WorkNode(
                "cli", "keep cli", owned_files=("pkg/cli.py",), verification_command=("true",)
            ),
            WorkNode("merge", "integrate", kind="integrate", depends_on=("parser", "cli")),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("run_started", run_id="run-nc", goal="already there")
    for node_id in ("parser", "cli"):
        log.emit("dispatch", node_id=node_id, attempt=1, route_id="cc/s")
        log.emit("terminal", node_id=node_id, attempt=1, ok=True, route_id="cc/s")
        log.emit("barrier", node_id=node_id, name="ownership", ok=True, detail="0 file(s)")
        log.emit(
            "barrier",
            node_id=node_id,
            name="no_change",
            ok=True,
            detail="no file changes; accepted only if verification passes",
        )
        log.emit("verify", node_id=node_id, ok=True, command=["true"], exit_code=0)
        log.emit("node_state", node_id=node_id, state="VALIDATED")
    log.emit("barrier", node_id="merge", name="integration", ok=True)
    log.emit("integrate", node_id="merge", commit="abc")
    log.emit("terminal", node_id="merge", attempt=1, ok=True, route_id="local/git")
    log.emit("verify", node_id="merge", ok=True, command=["true"], exit_code=0)
    log.emit("run_finished", outcome="COMPLETE", reason="done")
    (run_dir / "review.json").write_text(json.dumps(PASS))
    receipt = build_run_receipt(run_dir)
    assert receipt["no_change_nodes"] == ["parser", "cli"]
    path = write_run_receipt(run_dir)
    stored = json.loads(path.read_text())
    assert stored["no_change_nodes"] == ["parser", "cli"]
    assert verify_run_receipt(run_dir) == []


def test_no_change_nodes_omitted_when_empty(tmp_path: Path) -> None:
    receipt = build_run_receipt(_run(tmp_path, review=PASS))
    assert "no_change_nodes" not in receipt


def test_run_receipt_text_reports_no_change_nodes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """verdict run-receipt text names how many implement nodes changed no files."""
    from argparse import Namespace

    from verdict.orchestration.cli import _receipt

    test_no_change_nodes_listed_on_receipt(tmp_path)
    run_dir = tmp_path / "run"
    rc = _receipt(Namespace(run=str(run_dir), runs_dir=str(tmp_path), json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 implement nodes changed no files" in out


def test_no_change_unvalidated_implement_is_excluded(tmp_path: Path) -> None:
    """A no_change barrier on a node that did not validate is not summarized."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="g",
        nodes=(
            WorkNode(
                "parser",
                "keep parser",
                owned_files=("pkg/parser.py",),
                verification_command=("true",),
            ),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("run_started", run_id="run-nc", goal="g")
    log.emit("dispatch", node_id="parser", attempt=1, route_id="cc/s")
    log.emit("terminal", node_id="parser", attempt=1, ok=True, route_id="cc/s")
    log.emit("barrier", node_id="parser", name="ownership", ok=True)
    log.emit("barrier", node_id="parser", name="no_change", ok=True)
    log.emit("verify", node_id="parser", ok=False, command=["true"], exit_code=1)
    log.emit("run_finished", outcome="BLOCKED", reason="verify failed")
    receipt = build_run_receipt(run_dir)
    assert "no_change_nodes" not in receipt


# ---------------------------------------------------------------- BOD-225 producer provenance


def _producer_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    import subprocess

    import verdict

    repo = tmp_path / "source"
    package = repo / "verdict"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("# installed Verdict source\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    monkeypatch.setattr(verdict, "__file__", str(source))
    return repo, head


def test_capture_producer_shape_and_authoritative_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed source checkout is authoritative, not the process cwd."""
    import verdict

    repo, head = _producer_checkout(tmp_path, monkeypatch)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    producer = capture_producer()
    assert set(producer) == {"verdict_version", "git_sha", "dirty"}
    assert producer["verdict_version"] == verdict.__version__
    assert producer["git_sha"] == head
    assert producer["dirty"] is False

    (repo / "dirty.txt").write_text("dirty\n")
    dirty = capture_producer()
    assert dirty["dirty"] is True
    assert dirty["git_sha"] == head


@pytest.mark.parametrize("inside_checkout", [False, True])
def test_capture_producer_wheel_has_null_git_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside_checkout: bool
) -> None:
    """A wheel has no Git source identity, even inside another checkout's .venv."""
    import verdict

    repo, _ = _producer_checkout(tmp_path, monkeypatch)
    root = repo if inside_checkout else tmp_path / "wheel"
    package = root / ".venv/lib/site-packages/verdict"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("# wheel\n")
    monkeypatch.setattr(verdict, "__file__", str(source))
    monkeypatch.chdir(repo)
    producer = capture_producer()
    assert set(producer) == {"verdict_version", "git_sha", "dirty"}
    assert producer["git_sha"] is None
    assert producer["dirty"] is None


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_capture_producer_git_unavailable_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import subprocess

    from verdict.orchestration import receipt

    _producer_checkout(tmp_path, monkeypatch)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["timeout"] == 5
        if failure == "missing":
            raise FileNotFoundError("git")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(receipt.subprocess, "run", unavailable)
    producer = capture_producer()
    assert producer["git_sha"] is None
    assert producer["dirty"] is None


def test_capture_producer_version_unavailable_is_explicit_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata

    import verdict

    def unavailable(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.delattr(verdict, "__version__")
    monkeypatch.setattr(importlib.metadata, "version", unavailable)
    assert capture_producer()["verdict_version"] is None


def test_build_run_receipt_copies_producer_from_first_run_started(tmp_path: Path) -> None:
    """Receipt projects producer from the first run_started; never recomputes."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    original = {"verdict_version": "0.3.0", "git_sha": "abc123deadbeef", "dirty": False}
    log.emit("run_started", run_id="run-1", goal="add feature x", producer=original)
    # A later run_started (resume) with different values must not replace the original.
    log.emit(
        "run_started",
        run_id="run-1",
        goal="add feature x",
        producer={"verdict_version": "9.9.9", "git_sha": "ffffff", "dirty": True},
    )
    log.emit("run_finished", outcome="BLOCKED", reason="stop")
    receipt = build_run_receipt(run_dir)
    assert receipt["producer"] == original
    # Explicit nulls are preserved when present.
    run_dir2 = tmp_path / "run-nulls"
    run_dir2.mkdir()
    (run_dir2 / "graph.json").write_text(json.dumps(_graph().to_dict()))
    log2 = EventLog(run_dir2 / "events.jsonl", clock=_clock)
    nulls = {"verdict_version": None, "git_sha": None, "dirty": None}
    log2.emit("run_started", run_id="run-2", goal="add feature x", producer=nulls)
    assert build_run_receipt(run_dir2)["producer"] == nulls


def test_build_run_receipt_omits_producer_for_legacy_events(tmp_path: Path) -> None:
    """Legacy event logs without producer remain buildable and omit the field."""
    receipt = build_run_receipt(_run(tmp_path, review=PASS))
    assert "producer" not in receipt
    assert receipt["schema"] == RECEIPT_SCHEMA


def test_producer_receipt_verifies_and_tampering_is_detected(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS)
    events_path = run_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    original = {"verdict_version": "0.4.2", "git_sha": None, "dirty": None}
    events[0]["data"]["producer"] = original
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    write_run_receipt(run_dir)
    assert build_run_receipt(run_dir)["producer"] == original
    assert verify_run_receipt(run_dir) == []
    stored = json.loads((run_dir / "receipt.json").read_text())
    stored["producer"]["git_sha"] = "made-up"
    (run_dir / "receipt.json").write_text(json.dumps(stored))
    assert any("producer mismatch" in problem for problem in verify_run_receipt(run_dir))


def test_legacy_first_start_does_not_gain_later_producer(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, review=PASS)
    EventLog(run_dir / "events.jsonl").emit(
        "run_started", producer={"verdict_version": "new", "git_sha": "later", "dirty": True}
    )
    write_run_receipt(run_dir)
    assert "producer" not in build_run_receipt(run_dir)
    assert verify_run_receipt(run_dir) == []


def test_capture_producer_version_matches_imported_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metadata from another editable install cannot identify the imported source."""
    import importlib.metadata

    import verdict

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "unrelated-install")
    producer = capture_producer()
    assert producer["verdict_version"] == verdict.__version__
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_graph().to_dict()))
    EventLog(run_dir / "events.jsonl").emit("run_started", producer=producer)
    assert build_run_receipt(run_dir)["producer"]["verdict_version"] == verdict.__version__
    write_run_receipt(run_dir)
    assert verify_run_receipt(run_dir) == []


def test_capture_producer_version_metadata_fallback_only_without_source_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata

    import verdict

    monkeypatch.delattr(verdict, "__version__")
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "wheel-metadata")
    assert capture_producer()["verdict_version"] == "wheel-metadata"
