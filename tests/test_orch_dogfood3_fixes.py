"""Tests for dogfood-run-3 orchestration engine defects (BOD-263).

1. Receipt integrity on verdict-overridden runs (events_digest mismatch).
2. Prose barrier values from planner cause false BLOCKED completion_verdict.
3. Research nodes (owned_files=[]) must not commit files.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.orchestration.contracts import (
    KNOWN_BARRIERS,
    NodeKind,
    RunOutcome,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.planner import build_planning_prompt, hydrate_node_prompt
from verdict.orchestration.receipt import (
    EventLog,
    build_run_receipt,
    completion_verdict,
    verify_run_receipt,
    write_run_receipt,
)

FIXED = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return FIXED


# ------------------------------------------------------------------ Defect 1:
# Receipt integrity after VERDICT_OVERRIDE


def _build_overridden_run(tmp_path: Path) -> Path:
    """Simulate: runtime says COMPLETE, completion_verdict says BLOCKED.

    The controller emits VERDICT_OVERRIDE, then rewrites the receipt so that
    the final events_digest covers all events including the override.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="test receipt override",
        nodes=(
            WorkNode(
                "impl",
                "implement feature",
                owned_files=("pkg/a.py",),
                verification_command=("pytest", "-q", "tests/test_a.py"),
            ),
            WorkNode("merge", "integrate", kind="integrate", depends_on=("impl",)),
            WorkNode("rev", "review", kind="review", depends_on=("merge",)),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    # Node dispatched, succeeded, validated
    log.emit("run_started", run_id="run-ov", goal="test receipt override")
    log.emit("dispatch", node_id="impl", attempt=1, route_id="cc/a", capacity_class="sub")
    log.emit("terminal", node_id="impl", attempt=1, ok=True, duration_seconds=5.0)
    log.emit("verify", node_id="impl", ok=True, exit_code=0, command="pytest -q tests/test_a.py")
    log.emit("barrier", node_id="impl", name="ownership", ok=True, detail="1 file(s)")
    # Integration — barrier ok, with terminal+verify for the merge node
    log.emit("integrate", node_id="merge", commit="abc123")
    log.emit("barrier", name="integration", ok=True, detail="1 commit(s) merged")
    log.emit("terminal", node_id="merge", attempt=1, ok=True, route_id="local/git")
    log.emit("verify", node_id="merge", ok=True, command="pytest", exit_code=0)
    # Review — status ERROR, not PASS → completion_verdict returns BLOCKED
    log.emit("review", status="ERROR", reviewer="cc/b", route_id="cc/b", detail="timeout")
    log.emit("run_finished", outcome="COMPLETE", reason="all done", integration_ref="abc123")
    return run_dir


def test_receipt_override_integrity(tmp_path: Path) -> None:
    """After VERDICT_OVERRIDE event, rewriting the receipt makes digest valid."""
    run_dir = _build_overridden_run(tmp_path)
    # First receipt: runtime said COMPLETE but review is missing → BLOCKED.
    receipt_path = write_run_receipt(run_dir)
    receipt = json.loads(receipt_path.read_text())
    outcome, reason = completion_verdict(receipt)
    assert outcome == RunOutcome.BLOCKED.value
    assert "review" in reason.lower() or "MISSING" in reason

    # Simulate what run.py now does: emit override, then rewrite receipt.
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit(
        "controller",
        state="VERDICT_OVERRIDE",
        detail=f"runtime=COMPLETE receipt={outcome}: {reason}",
    )
    # Rewrite receipt to cover the new event
    receipt_path = write_run_receipt(run_dir)
    receipt2 = json.loads(receipt_path.read_text())

    # The receipt must now verify cleanly
    problems = verify_run_receipt(run_dir)
    assert problems == [], f"receipt verification failed: {problems}"
    # The override event is counted
    assert receipt2["event_count"] == receipt["event_count"] + 1
    # The outcome is still BLOCKED (override does not change logic)
    assert receipt2["outcome"] == RunOutcome.BLOCKED.value


# ------------------------------------------------------------------ Defect 2:
# Prose in barrier field → false BLOCKED from completion_verdict


def test_prose_barrier_normalized_to_empty() -> None:
    """A WorkNode with a prose barrier string normalizes to empty string."""
    node = WorkNode(
        "impl",
        "implement something",
        owned_files=("pkg/x.py",),
        verification_command=("pytest", "-q", "tests/t.py"),
        barrier="Requires careful understanding of HTTP status codes",
    )
    assert node.barrier == "", "prose barrier must be normalized to empty"


def test_known_barriers_kept() -> None:
    """Known barrier names survive normalization."""
    for name in KNOWN_BARRIERS:
        node = WorkNode(
            "impl",
            "implement something",
            owned_files=("pkg/x.py",),
            verification_command=("pytest", "-q", "tests/t.py"),
            barrier=name,
        )
        assert node.barrier == name


def test_boolean_barrier_normalization() -> None:
    """barrier=True → 'integration', barrier=False → ''."""
    node_true = WorkNode(
        "impl",
        "obj",
        owned_files=("a.py",),
        verification_command=("pytest",),
        barrier=True,  # type: ignore[arg-type]
    )
    assert node_true.barrier == "integration"
    node_false = WorkNode(
        "impl",
        "obj",
        owned_files=("a.py",),
        verification_command=("pytest",),
        barrier=False,  # type: ignore[arg-type]
    )
    assert node_false.barrier == ""


def test_df263_graph_barriers_sanitized() -> None:
    """Loading the real df263 graph.json: prose barriers become empty."""
    graph_path = Path("/tmp/dogfood-runs3/df263/20260928T140655Z/graph.json")
    if not graph_path.exists():
        pytest.skip("df263 graph.json not available")
    raw = json.loads(graph_path.read_text())
    graph = WorkGraph.from_dict(raw)
    for node in graph.nodes:
        assert node.barrier in KNOWN_BARRIERS or node.barrier == "", (
            f"node {node.node_id} has unsanitized barrier: {node.barrier!r}"
        )


def test_completion_verdict_with_only_known_barriers(tmp_path: Path) -> None:
    """A receipt whose graph has only known barriers passes completion_verdict cleanly."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="test barriers",
        nodes=(
            WorkNode(
                "impl",
                "implement",
                owned_files=("pkg/a.py",),
                verification_command=("pytest", "-q", "tests/t.py"),
                barrier="integration",
            ),
            WorkNode("merge", "integrate", kind="integrate", depends_on=("impl",)),
            WorkNode("rev", "review", kind="review", depends_on=("merge",)),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("run_started", run_id="run-b", goal="test barriers")
    log.emit("dispatch", node_id="impl", attempt=1, route_id="cc/a", capacity_class="sub")
    log.emit("terminal", node_id="impl", attempt=1, ok=True, duration_seconds=3.0)
    log.emit("verify", node_id="impl", ok=True, exit_code=0, command="pytest -q tests/t.py")
    log.emit("barrier", node_id="impl", name="ownership", ok=True, detail="1 file")
    log.emit("barrier", node_id="impl", name="integration", ok=True, detail="ok")
    # Integration merge node
    log.emit("integrate", node_id="merge", commit="sha1")
    log.emit("barrier", name="integration", ok=True, detail="merged")
    log.emit("terminal", node_id="merge", attempt=1, ok=True, route_id="local/git")
    log.emit("verify", node_id="merge", ok=True, command="pytest", exit_code=0)
    # Review
    (run_dir / "review.json").write_text(
        json.dumps({"status": "PASS", "reviewer": "ocr v1", "route_id": "cc/b", "findings": []})
    )
    log.emit("run_finished", outcome="COMPLETE", reason="done", integration_ref="sha1")

    receipt = build_run_receipt(run_dir)
    outcome, reason = completion_verdict(receipt)
    assert outcome == RunOutcome.COMPLETE.value, f"expected COMPLETE: {reason}"


# ------------------------------------------------------------------ Defect 3:
# Research nodes must not commit files


def test_research_node_no_owned_files() -> None:
    """Research node is created with empty owned_files."""
    node = WorkNode("research", "investigate", kind="research", coding=False)
    assert node.owned_files == ()
    assert node.kind is NodeKind.RESEARCH


def test_planner_prompt_mentions_barrier_and_research() -> None:
    """The planning prompt warns about barrier values and research file rules."""
    prompt = build_planning_prompt("test goal", "file_a.py", "")
    assert "integration" in prompt
    assert "ownership" in prompt
    assert "no_change" in prompt
    assert "research" in prompt.lower()
    assert "owned_files=[]" in prompt or "owned_files" in prompt


def test_hydrate_research_node_no_file_writes() -> None:
    """Worker prompt for a research node tells it not to write files."""
    node = WorkNode("research", "investigate things", kind="research", coding=False)
    prompt = hydrate_node_prompt(node, repo=Path("/tmp"), goal="test")
    assert "do not create" in prompt.lower() or "DO NOT create" in prompt
    assert "OWNED_FILES: (none)" in prompt


def test_hydrate_implement_node_mentions_owned_files() -> None:
    """Worker prompt for an implement node still lists its owned files."""
    node = WorkNode(
        "impl", "build", owned_files=("pkg/a.py",), verification_command=("pytest", "-q", "t.py")
    )
    prompt = hydrate_node_prompt(node, repo=Path("/tmp"), goal="test")
    assert "pkg/a.py" in prompt
    assert "Only edit files" in prompt


def test_known_barriers_constant() -> None:
    """KNOWN_BARRIERS is a frozenset with the runtime barrier names."""
    assert isinstance(KNOWN_BARRIERS, frozenset)
    assert "integration" in KNOWN_BARRIERS
    assert "ownership" in KNOWN_BARRIERS
    assert "no_change" in KNOWN_BARRIERS
