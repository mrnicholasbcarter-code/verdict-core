"""Resume keeps reviewer independence: implementers from a previous controller life stay excluded."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

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
from verdict.orchestration.receipt import EventLog
from verdict.orchestration.run import prior_validated, run_golden_path


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

    def record_failure(self, route_id: str, f: FailureClassification, *, now: datetime) -> None: ...

    def record_success(self, route_id: str, *, now: datetime) -> None: ...


class _Exec:
    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        raise AssertionError("validated node must not be re-executed on resume")


class _Classifier:
    def classify(self, t: WorkerTerminal, *, now: datetime) -> FailureClassification:
        return FailureClassification("unknown", "REROUTE", 1, "route")


class _Reviewer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def review(self, **kwargs: Any) -> ReviewResult:
        self.calls.append(kwargs)
        return ReviewResult("PASS", "fake", "cx/rev")


async def test_resume_restores_implementer_routes_for_review_independence(tmp_path: Path) -> None:
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
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    log = EventLog(run_dir / "events.jsonl")
    # A real previous controller life: dispatch -> terminal ok -> verify ok -> VALIDATED.
    log.emit("dispatch", node_id="a", attempt=1, route_id="cc/implementer")
    log.emit("terminal", node_id="a", attempt=1, ok=True, route_id="cc/implementer")
    log.emit("node_state", node_id="a", state="TERMINAL_SUCCESS", route_id="cc/implementer")
    log.emit("verify", node_id="a", ok=True, exit_code=0, command="true")
    log.emit("node_state", node_id="a", state="VALIDATED", route_id="cc/implementer", commit=head)
    assert prior_validated(run_dir) == {"a": (head, "cc/implementer")}
    reviewer = _Reviewer()
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=runs,
        selector=_Sel(),
        executor=_Exec(),
        classifier=_Classifier(),
        reviewer=reviewer,
        graph=graph,
        run_id="r1",
    )
    assert result.outcome == "COMPLETE", result.reason
    assert "cc/implementer" in reviewer.calls[0]["exclude_routes"]
