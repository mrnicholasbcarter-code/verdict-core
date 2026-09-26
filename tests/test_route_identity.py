"""Tests for route identity tracking in run receipts: route identity tracking in run receipts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.receipt import EventLog, build_run_receipt

FIXED = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return FIXED


def _simple_graph() -> WorkGraph:
    return WorkGraph(
        goal="test route identity",
        nodes=(
            WorkNode(
                "task",
                "do work",
                owned_files=("pkg/task.py",),
                verification_command=("pytest", "-q", "tests/test_task.py"),
            ),
        ),
    )


def test_route_identity_match(tmp_path: Path) -> None:
    """Route identity is 'match' when reported_model equals route_id (successful terminal)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=True,
        route_id="cc/claude-sonnet-5",
        reported_model="cc/claude-sonnet-5",
        duration_seconds=2.0,
    )
    log.emit("verify", node_id="task", ok=True, command=["pytest"], exit_code=0)
    log.emit("run_finished", outcome="COMPLETE", reason="done")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    assert len(task_node["attempts"]) == 1
    attempt = task_node["attempts"][0]

    assert attempt["intended_route"] == "cc/claude-sonnet-5"
    assert attempt["executed_model"] == "cc/claude-sonnet-5"
    assert attempt["route_identity"] == "match"
    assert attempt["outcome"] == "success"

    # Check run-level summary
    summary = receipt["route_identity_summary"]
    assert summary["attempts"] == 1
    assert summary["match"] == 1
    assert summary["mismatch"] == 0
    assert summary["unattested"] == 0
    assert summary["mechanical"] == 0

    # No warning since there's no mismatch
    assert "route_identity_warning" not in receipt


def test_route_identity_mismatch(tmp_path: Path) -> None:
    """Route identity is 'mismatch' when reported_model differs from route_id (successful terminal)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=True,
        route_id="cc/claude-sonnet-5",
        reported_model="cc/claude-opus-5",  # Different model!
        duration_seconds=2.0,
    )
    log.emit("verify", node_id="task", ok=True, command=["pytest"], exit_code=0)
    log.emit("run_finished", outcome="COMPLETE", reason="done")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    attempt = task_node["attempts"][0]

    assert attempt["intended_route"] == "cc/claude-sonnet-5"
    assert attempt["executed_model"] == "cc/claude-opus-5"
    assert attempt["route_identity"] == "mismatch"
    assert attempt["outcome"] == "success"

    # Check run-level summary
    summary = receipt["route_identity_summary"]
    assert summary["attempts"] == 1
    assert summary["match"] == 0
    assert summary["mismatch"] == 1

    # Warning present since successful attempt had mismatch
    assert "route_identity_warning" in receipt
    assert "different from the intended route" in receipt["route_identity_warning"]


def test_route_identity_unattested_no_reported_model(tmp_path: Path) -> None:
    """Route identity is 'unattested' when reported_model is absent or empty (successful terminal)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    # No reported_model in terminal event
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=True,
        route_id="cc/claude-sonnet-5",
        duration_seconds=2.0,
    )
    log.emit("verify", node_id="task", ok=True, command=["pytest"], exit_code=0)
    log.emit("run_finished", outcome="COMPLETE", reason="done")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    attempt = task_node["attempts"][0]

    assert attempt["intended_route"] == "cc/claude-sonnet-5"
    assert attempt["executed_model"] is None
    assert attempt["route_identity"] == "unattested"
    assert attempt["outcome"] == "success"

    summary = receipt["route_identity_summary"]
    assert summary["unattested"] == 1


def test_route_identity_mechanical_merge(tmp_path: Path) -> None:
    """Route identity is 'mechanical' for mechanical merge attempts."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="test",
        nodes=(
            WorkNode("a", "build a", owned_files=("a.py",), verification_command=("pytest",)),
            WorkNode("b", "build b", owned_files=("b.py",), verification_command=("pytest",)),
            WorkNode("merge", "integrate", kind="integrate", depends_on=("a", "b")),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    # Simulate mechanical merge
    log.emit(
        "terminal",
        node_id="merge",
        attempt=1,
        ok=True,
        route_id="",
        reported_model="(mechanical merge)",
        duration_seconds=0.0,
    )
    log.emit("run_finished", outcome="COMPLETE", reason="done")

    receipt = build_run_receipt(run_dir)

    merge_node = next(n for n in receipt["nodes"] if n["node_id"] == "merge")
    attempt = merge_node["attempts"][0]

    assert attempt["route_identity"] == "mechanical"
    assert attempt["executed_model"] == "(mechanical merge)"

    summary = receipt["route_identity_summary"]
    assert summary["mechanical"] == 1


def test_route_identity_failed_terminal_timeout_unattested(tmp_path: Path) -> None:
    """Failed terminal (ok=False) with timeout is 'unattested' even if model == route_id.

    This tests the route identity tracking fix: executors.py sets model=route_id on timeout failures,
    but since there was no actual harness attestation, route_identity must be "unattested".
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    # Failed terminal with model == route_id (executor fallback)
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=False,
        route_id="cc/claude-sonnet-5",
        reported_model="cc/claude-sonnet-5",  # Executor sets this, but not attested
        error="timeout",
        duration_seconds=60.0,
    )
    log.emit("failure", node_id="task", attempt=1, category="timeout")
    log.emit("run_finished", outcome="INCOMPLETE", reason="timeout")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    attempt = task_node["attempts"][0]

    assert attempt["intended_route"] == "cc/claude-sonnet-5"
    assert attempt["executed_model"] == "cc/claude-sonnet-5"
    # Despite model == route_id, this is unattested because ok=False and error != model_mismatch
    assert attempt["route_identity"] == "unattested"
    assert attempt["outcome"] == "failure"
    assert attempt["failure_category"] == "timeout"

    summary = receipt["route_identity_summary"]
    assert summary["unattested"] == 1
    assert summary["match"] == 0


def test_route_identity_failed_terminal_model_mismatch(tmp_path: Path) -> None:
    """Failed terminal (ok=False) with error='model_mismatch' is classified as 'mismatch'.

    This is the special case where even a failed terminal can be classified as mismatch.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    # Failed terminal with model_mismatch error
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=False,
        route_id="cc/claude-sonnet-5",
        reported_model="cc/claude-opus-5",  # Different model
        error="model_mismatch",
        duration_seconds=1.0,
    )
    log.emit("failure", node_id="task", attempt=1, category="model_mismatch")
    log.emit("run_finished", outcome="INCOMPLETE", reason="model_mismatch")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    attempt = task_node["attempts"][0]

    assert attempt["intended_route"] == "cc/claude-sonnet-5"
    assert attempt["executed_model"] == "cc/claude-opus-5"
    # Model mismatch is detected even for failed terminals
    assert attempt["route_identity"] == "mismatch"
    assert attempt["outcome"] == "failure"
    assert attempt["failure_category"] == "model_mismatch"

    summary = receipt["route_identity_summary"]
    assert summary["mismatch"] == 1
    # No warning: only successful mismatches trigger the warning
    assert "route_identity_warning" not in receipt


def test_route_identity_failover_match_after_mismatch(tmp_path: Path) -> None:
    """Multiple attempts: first mismatches and fails, second matches and succeeds."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")

    # Attempt 1: model_mismatch failure
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=False,
        route_id="cc/claude-sonnet-5",
        reported_model="cc/claude-opus-5",
        error="model_mismatch",
        duration_seconds=1.0,
    )
    log.emit("failure", node_id="task", attempt=1, category="model_mismatch")

    # Attempt 2: successful match
    log.emit("dispatch", node_id="task", attempt=2, route_id="gh/gpt-5", capacity_class="free")
    log.emit(
        "terminal",
        node_id="task",
        attempt=2,
        ok=True,
        route_id="gh/gpt-5",
        reported_model="gh/gpt-5",
        duration_seconds=2.0,
    )
    log.emit("verify", node_id="task", ok=True, command=["pytest"], exit_code=0)
    log.emit("run_finished", outcome="COMPLETE", reason="done")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    assert len(task_node["attempts"]) == 2

    # Attempt 1: mismatch
    attempt1 = task_node["attempts"][0]
    assert attempt1["route_identity"] == "mismatch"
    assert attempt1["outcome"] == "failure"

    # Attempt 2: match
    attempt2 = task_node["attempts"][1]
    assert attempt2["route_identity"] == "match"
    assert attempt2["outcome"] == "success"

    # Summary
    summary = receipt["route_identity_summary"]
    assert summary["attempts"] == 2
    assert summary["match"] == 1
    assert summary["mismatch"] == 1

    # No warning: the successful attempt matched
    assert "route_identity_warning" not in receipt


def test_route_identity_legacy_events_no_reported_model(tmp_path: Path) -> None:
    """Legacy events.jsonl without reported_model field should not crash."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-1", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )
    # Simulate old event format: no reported_model, no error
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=True,
        route_id="cc/claude-sonnet-5",
        duration_seconds=2.0,
        # reported_model and error fields missing
    )
    log.emit("verify", node_id="task", ok=True, command=["pytest"], exit_code=0)
    log.emit("run_finished", outcome="COMPLETE", reason="done")

    # Should not crash
    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    attempt = task_node["attempts"][0]

    # Should default to unattested
    assert attempt["route_identity"] == "unattested"
    assert attempt["executed_model"] is None


def test_route_identity_e2e_runtime_emits_error(tmp_path: Path) -> None:
    """End-to-end: verify that runtime emits error field and receipt can process it correctly."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(_simple_graph().to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)

    log.emit("run_started", run_id="run-e2e", goal="test")
    log.emit(
        "dispatch", node_id="task", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="free"
    )

    # Simulate what runtime.py now emits: terminal with error field
    log.emit(
        "terminal",
        node_id="task",
        attempt=1,
        ok=False,
        route_id="cc/claude-sonnet-5",
        reported_model="cc/claude-opus-5",  # Different model reported
        error="model_mismatch",  # Error field from WorkerTerminal
        duration_seconds=1.5,
        session_ref="session-123",
        stop_reason="",
        fault_injected=False,
    )
    log.emit("failure", node_id="task", attempt=1, category="model_mismatch")
    log.emit("run_finished", outcome="INCOMPLETE", reason="model_mismatch")

    receipt = build_run_receipt(run_dir)

    task_node = receipt["nodes"][0]
    attempt = task_node["attempts"][0]

    # Should be classified as mismatch because of the error field
    assert attempt["route_identity"] == "mismatch"
    assert attempt["intended_route"] == "cc/claude-sonnet-5"
    assert attempt["executed_model"] == "cc/claude-opus-5"
    assert attempt["outcome"] == "failure"


def test_route_identity_real_e2e_with_runtime(tmp_path: Path) -> None:
    """Real end-to-end test through DagRuntime with fake executor reporting model_mismatch.

    This verifies that the receipt correctly classifies route identity for failed terminals.
    The classification uses failure_category from the failure event (always emitted after
    a failed terminal), making the terminal.error field redundant for classification but
    useful as provenance.
    """
    import subprocess

    from verdict.orchestration.contracts import (
        CapacityClass,
        EligibilityStage,
        FailureClassification,
        RouteVerdict,
        RunOutcome,
        TaskRequirements,
        WorkerTerminal,
    )
    from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

    # Set up repo
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    # Graph with one task
    graph = WorkGraph(
        goal="test route identity",
        nodes=(
            WorkNode(
                "task",
                "implement task",
                owned_files=("task.txt",),
                verification_command=("cat", "task.txt"),
            ),
        ),
    )

    # Fake selector that offers two routes
    class FakeSelector:
        def select(
            self, requirements: TaskRequirements, *, now: datetime
        ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
            routes = ["cc/claude-sonnet-5", "gh/gpt-5"]
            verdicts = []
            chosen = None
            for rank, route in enumerate(routes, 1):
                provider = route.split("/")[0]
                if route in requirements.exclude_routes:
                    continue
                v = RouteVerdict(
                    route,
                    provider,
                    EligibilityStage.SELECTED,
                    None,
                    "ok",
                    CapacityClass.FREE,
                    rank=rank,
                )
                verdicts.append(v)
                if chosen is None:
                    chosen = v
            return chosen, tuple(verdicts)

        def record_failure(
            self, route_id: str, failure: FailureClassification, *, now: datetime
        ) -> None:
            pass

        def record_success(self, route_id: str, *, now: datetime) -> None:
            pass

    # Fake classifier
    class FakeClassifier:
        def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification:
            if terminal.error == "model_mismatch":
                return FailureClassification(
                    "model_mismatch", "REROUTE", 0, "route", terminal.error
                )
            return FailureClassification("unknown", "REROUTE", 0, "route", terminal.error)

    # Fake executor that returns model_mismatch on attempt 1, success on attempt 2
    class FakeExecutor:
        def __init__(self) -> None:
            self.call_count = 0

        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            self.call_count += 1
            if self.call_count == 1:
                # First attempt: model_mismatch (ok=False, different model, error set)
                return WorkerTerminal(
                    ok=False,
                    model="other/wrong-model",
                    error="model_mismatch",
                    duration_seconds=1.0,
                )
            else:
                # Second attempt: success with correct model
                (cwd / "task.txt").write_text("done\n")
                return WorkerTerminal(
                    ok=True,
                    output="RESULT: DONE",
                    model=route_id,  # Reports the correct model
                    stop_reason="stop",
                    duration_seconds=2.0,
                )

    # Fake reviewer
    class FakeReviewer:
        async def review(self, **kwargs: Any) -> Any:
            from verdict.orchestration.contracts import ReviewResult

            return ReviewResult("PASS", "fake", "fake/reviewer", ())

    # Run the runtime
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))

    events = EventLog(run_dir / "events.jsonl")
    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=graph,
        selector=FakeSelector(),
        executor=FakeExecutor(),
        classifier=FakeClassifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=FakeReviewer(),
        policy=RuntimePolicy(),
    )

    import asyncio

    result = asyncio.run(runtime.run())
    assert result.outcome is RunOutcome.COMPLETE, result.reason

    # Build receipt from the actual events
    receipt = build_run_receipt(run_dir)

    # Verify the receipt
    task_node = receipt["nodes"][0]
    assert len(task_node["attempts"]) == 2

    # Attempt 1: model_mismatch with ok=False
    attempt1 = task_node["attempts"][0]
    assert attempt1["intended_route"] == "cc/claude-sonnet-5"
    assert attempt1["executed_model"] == "other/wrong-model"
    assert attempt1["route_identity"] == "mismatch"  # This is the key assertion!
    assert attempt1["outcome"] == "failure"
    assert attempt1["failure_category"] == "model_mismatch"

    # Attempt 2: success with matching model
    attempt2 = task_node["attempts"][1]
    assert attempt2["intended_route"] == "gh/gpt-5"
    assert attempt2["executed_model"] == "gh/gpt-5"
    assert attempt2["route_identity"] == "match"
    assert attempt2["outcome"] == "success"

    # Summary
    summary = receipt["route_identity_summary"]
    assert summary["attempts"] == 2
    assert summary["match"] == 1
    assert summary["mismatch"] == 1
    assert summary["unattested"] == 0
    assert summary["mechanical"] == 0

    # No warning: the successful attempt matched
    assert "route_identity_warning" not in receipt
