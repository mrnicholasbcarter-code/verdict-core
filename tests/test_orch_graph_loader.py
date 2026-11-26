"""Who may load an integrate or review node that owns files (#1356).

A fresh graph (``verdict orchestrate --graph``) is validated strictly: the node's authoring work
would never be dispatched. A graph read back from a recorded run (resume, controls, receipts) keeps
loading unchanged, so old runs still resume and their receipts still verify. The two are told apart
by an explicit loader option, never by a default.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    OrchestrationError,
    ReviewResult,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
)
from verdict.orchestration.controls import RunControl
from verdict.orchestration.receipt import EventLog
from verdict.orchestration.run import run_golden_path


def _historical_graph(kind: str = "integrate") -> dict[str, Any]:
    """A graph recorded before the merge-only rule: a merge node that owns a test file."""
    return {
        "goal": "ship it",
        "nodes": [
            {
                "node_id": "impl",
                "objective": "implement",
                "kind": "implement",
                "owned_files": ["impl.txt"],
                "verification_command": ["true"],
            },
            {
                "node_id": "merge",
                "objective": "write the end to end test",
                "kind": kind,
                "depends_on": ["impl"],
                "owned_files": ["tests/test_e2e.py"],
                "verification_command": ["true"],
            },
        ],
    }


@pytest.mark.parametrize("kind", ["integrate", "review"])
def test_a_fresh_graph_is_strict_by_default(kind: str) -> None:
    with pytest.raises(OrchestrationError, match="must not have owned_files") as raised:
        WorkGraph.from_dict(_historical_graph(kind))
    assert "merge:" in str(raised.value)
    assert "implement node" in str(raised.value)


@pytest.mark.parametrize("kind", ["integrate", "review"])
def test_a_recorded_graph_loads_unchanged_with_the_explicit_option(kind: str) -> None:
    graph = WorkGraph.from_dict(_historical_graph(kind), allow_merge_only_owned_files=True)
    assert graph.node("merge").owned_files == ("tests/test_e2e.py",)
    again = WorkGraph.from_dict(graph.to_dict(), allow_merge_only_owned_files=True)
    assert again.digest() == graph.digest()
    # The option relaxes nothing else: an implement node still needs its files.
    broken = _historical_graph(kind)
    broken["nodes"][0]["owned_files"] = []
    with pytest.raises(OrchestrationError, match="needs owned_files"):
        WorkGraph.from_dict(broken, allow_merge_only_owned_files=True)


def _orchestrate_args(tmp_path: Path, graph_file: Path) -> argparse.Namespace:
    return argparse.Namespace(
        repo=str(tmp_path),
        runs_dir=str(tmp_path / "runs"),
        goal="test",
        graph=str(graph_file),
        resume=None,
        executor="prime",
        executor_map=None,
        gateway="http://127.0.0.1:20128",
        scope="",
        prefer="",
        state_file=None,
        inject=[],
        max_parallel=1,
        attempt_timeout=900.0,
        run_deadline=3600.0,
        no_review=True,
        plain=True,
        json=False,
        node=None,
        panel=None,
    )


def test_orchestrate_refuses_a_fresh_graph_whose_integrate_node_owns_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--graph`` is a fresh graph: the merge-only rule applies, and no run starts."""
    from verdict.orchestration.cli import _orchestrate

    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(_historical_graph()))
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-for-validation")

    code = _orchestrate(_orchestrate_args(tmp_path, graph_file))

    assert code == 2
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "must not have owned_files" in err
    assert "merge" in err
    assert not (tmp_path / "runs").exists(), "no run directory may be created"


def _record_run(run_dir: Path, graph: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "graph.json").write_text(json.dumps({"run_id": run_dir.name, **graph}))
    EventLog(run_dir / "events.jsonl").emit("run_started")


def test_run_controls_read_a_recorded_graph_that_has_an_integrate_node_with_files(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r1"
    _record_run(run_dir, _historical_graph())

    request = RunControl(run_dir).submit("cancel_run")

    assert request.kind == "cancel_run"


class _Selector:
    def evaluate(self, r: TaskRequirements, *, now: datetime) -> tuple[RouteVerdict, ...]:
        return ()

    def select(
        self, r: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        verdict = RouteVerdict(
            "cc/x", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION, rank=0
        )
        return verdict, (verdict,)

    def record_failure(self, route_id: str, f: FailureClassification, *, now: datetime) -> None: ...

    def record_success(self, route_id: str, *, now: datetime) -> None: ...


class _Executor:
    """Writes the file the implement node owns; the merge node needs no worker at all."""

    def __init__(self) -> None:
        self.nodes: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.nodes.append(prompt)
        (cwd / "impl.txt").write_text("impl\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


class _Classifier:
    def classify(self, t: WorkerTerminal, *, now: datetime) -> FailureClassification:
        return FailureClassification("unknown", "REROUTE", 1, "route")


class _Reviewer:
    async def review(self, **kwargs: Any) -> ReviewResult:
        return ReviewResult("PASS", "fake", "cx/rev")


@pytest.mark.asyncio
async def test_resume_reads_a_recorded_graph_that_has_an_integrate_node_with_files(
    tmp_path: Path,
) -> None:
    """``run_golden_path`` without a supplied graph reloads ``graph.json`` of the run it resumes."""
    import subprocess

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
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    runs = tmp_path / "runs"
    _record_run(runs / "r1", _historical_graph())

    executor = _Executor()
    result = await run_golden_path(
        "ship it",
        repo=repo,
        runs_root=runs,
        run_id="r1",
        selector=_Selector(),
        executor=executor,
        classifier=_Classifier(),
        reviewer=_Reviewer(),
    )

    assert "must not have owned_files" not in result.reason
    assert result.outcome == "COMPLETE", result.reason
    assert len(executor.nodes) == 1, "the implement node ran; the merge node got no worker"
