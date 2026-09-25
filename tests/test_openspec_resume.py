"""Test OpenSpec spec digest resume behavior through the real resume harness."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from verdict.openspec_lifecycle import OpenSpecChange, spec_revision_digest
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    ReviewResult,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.run import SPEC_CHANGED, run_golden_path


class _Sel:
    def evaluate(self, r: TaskRequirements, *, now: datetime) -> tuple[RouteVerdict, ...]:
        return ()

    def select(
        self, r: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        v = RouteVerdict(
            "cc/x", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION, rank=0
        )
        return v, (v,)

    def record_failure(self, route_id: str, f: FailureClassification, *, now: datetime) -> None:
        pass

    def record_success(self, route_id: str, *, now: datetime) -> None:
        pass


class _Exec:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.calls += 1
        return WorkerTerminal(ok=True, output="done", commit="abc123", duration=1.0)


class _NoExec:
    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        raise AssertionError("executor must not be called")


class _Classifier:
    def classify(self, t: WorkerTerminal, *, now: datetime) -> FailureClassification:
        return FailureClassification("unknown", "REROUTE", 1, "route")


class _Reviewer:
    async def review(self, **kwargs: Any) -> ReviewResult:
        return ReviewResult("PASS", "fake", "cx/rev")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)
    return repo


async def test_resume_blocks_on_spec_change(tmp_path: Path) -> None:
    """Resume with changed digest blocks execution."""
    repo = _make_repo(tmp_path)
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)

    # Create change_dir
    change_dir = repo / "openspec" / "changes" / "test-change"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("# Test\n## Why\nOriginal\n")

    # Compute digest
    change = OpenSpecChange(
        change_id="test-change",
        linear_issue=None,
        schema="verdict-change-v1",
        change_dir=change_dir,
        artifacts={},
    )
    digest = spec_revision_digest(change)

    # Create graph.json with openspec block
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    graph_data = {
        **graph.to_dict(),
        "run_id": "r1",
        "openspec": {
            "linear_issue": "TEST-1",
            "openspec_change": "test-change",
            "openspec_schema": "verdict-change-v1",
            "change_dir": str(change_dir),
            "spec_revision_digest": digest,
            "current_task": None,
            "completed_tasks": [],
            "conformance_result": None,
        },
    }
    (run_dir / "graph.json").write_text(json.dumps(graph_data))
    (run_dir / "events.jsonl").write_text("")

    # Edit one byte
    (change_dir / "proposal.md").write_text("# Test\n## Why\nModified\n")

    # Resume
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_NoExec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r1",
    )

    assert result.outcome == "BLOCKED"
    assert SPEC_CHANGED in result.reason


async def test_resume_unchanged_keeps_block(tmp_path: Path) -> None:
    """Resume twice with unchanged digest preserves block, then edit blocks."""
    repo = _make_repo(tmp_path)
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)

    # Create change_dir
    change_dir = repo / "openspec" / "changes" / "test-change"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("# Test\n## Why\nOriginal\n")

    # Compute digest
    change = OpenSpecChange(
        change_id="test-change",
        linear_issue=None,
        schema="verdict-change-v1",
        change_dir=change_dir,
        artifacts={},
    )
    original_digest = spec_revision_digest(change)

    # Create graph.json with openspec block
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    original_block = {
        "linear_issue": "TEST-1",
        "openspec_change": "test-change",
        "openspec_schema": "verdict-change-v1",
        "change_dir": str(change_dir),
        "spec_revision_digest": original_digest,
        "current_task": None,
        "completed_tasks": [],
        "conformance_result": None,
    }
    graph_data = {**graph.to_dict(), "run_id": "r1", "openspec": original_block}
    (run_dir / "graph.json").write_text(json.dumps(graph_data))
    (run_dir / "events.jsonl").write_text("")

    # Resume 1
    await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_Exec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r1",
    )

    # Block still there
    graph_after_1 = json.loads((run_dir / "graph.json").read_text())
    assert graph_after_1["openspec"] == original_block

    # Resume 2
    await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_Exec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r1",
    )

    # Block still there
    graph_after_2 = json.loads((run_dir / "graph.json").read_text())
    assert graph_after_2["openspec"] == original_block

    # Edit one byte
    (change_dir / "proposal.md").write_text("# Test\n## Why\nModified\n")

    # Resume 3 -> BLOCKED
    result3 = await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_NoExec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r1",
    )

    assert result3.outcome == "BLOCKED"
    assert SPEC_CHANGED in result3.reason


@pytest.mark.parametrize(
    "bad_block",
    [{}, {"change_dir": ""}, {"change_dir": "PLACEHOLDER", "spec_revision_digest": ""}, "notadict"],
)
async def test_resume_fail_closed_bad_block(tmp_path: Path, bad_block: Any) -> None:
    """Resume with malformed block fails closed."""
    repo = _make_repo(tmp_path)
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)

    # Create change_dir (may not be used)
    change_dir = repo / "openspec" / "changes" / "test-change"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("# Test\n")

    # Replace PLACEHOLDER with actual path in parametrized cases
    if isinstance(bad_block, dict) and bad_block.get("change_dir") == "PLACEHOLDER":
        bad_block = {**bad_block, "change_dir": str(change_dir)}

    # Create graph.json with bad block
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    graph_data = {**graph.to_dict(), "run_id": "r1", "openspec": bad_block}
    (run_dir / "graph.json").write_text(json.dumps(graph_data))
    (run_dir / "events.jsonl").write_text("")

    # Resume
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_NoExec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r1",
    )

    assert result.outcome == "BLOCKED"
    assert SPEC_CHANGED in result.reason


async def test_legacy_graph_without_block_unchanged(tmp_path: Path) -> None:
    """Resume of legacy graph without openspec block behaves normally."""
    repo = _make_repo(tmp_path)
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)

    # Create graph.json WITHOUT openspec block
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    graph_data = {**graph.to_dict(), "run_id": "r1"}
    (run_dir / "graph.json").write_text(json.dumps(graph_data))
    (run_dir / "events.jsonl").write_text("")

    # Resume
    executor = _Exec()
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=executor,
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r1",
    )

    # Executor WAS reached
    assert executor.calls > 0
    # SPEC_CHANGED not in reason
    assert SPEC_CHANGED not in result.reason


async def test_resume_with_explicit_graph_still_checks(tmp_path: Path) -> None:
    """Resume with explicit graph= and edited spec still blocks."""
    repo = _make_repo(tmp_path)
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)

    # Create change_dir
    change_dir = repo / "openspec" / "changes" / "test-change"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("# Test\n## Why\nOriginal\n")

    # Compute digest
    change = OpenSpecChange(
        change_id="test-change",
        linear_issue=None,
        schema="verdict-change-v1",
        change_dir=change_dir,
        artifacts={},
    )
    digest = spec_revision_digest(change)

    # Create graph.json with openspec block
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    graph_data = {
        **graph.to_dict(),
        "run_id": "r1",
        "openspec": {
            "linear_issue": "TEST-1",
            "openspec_change": "test-change",
            "openspec_schema": "verdict-change-v1",
            "change_dir": str(change_dir),
            "spec_revision_digest": digest,
            "current_task": None,
            "completed_tasks": [],
            "conformance_result": None,
        },
    }
    (run_dir / "graph.json").write_text(json.dumps(graph_data))
    (run_dir / "events.jsonl").write_text("")

    # Edit one byte
    (change_dir / "proposal.md").write_text("# Test\n## Why\nModified\n")

    # Resume with EXPLICIT graph=
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_NoExec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        graph=graph,
        run_id="r1",
    )

    assert result.outcome == "BLOCKED"
    assert SPEC_CHANGED in result.reason
