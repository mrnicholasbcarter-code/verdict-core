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
from verdict.orchestration.receipt import EventLog, build_run_receipt, capture_producer
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


def test_prior_attempts_continue_numbering(tmp_path: Path) -> None:
    from verdict.orchestration.run import prior_attempts

    run_dir = tmp_path / "r"
    run_dir.mkdir()
    log = EventLog(run_dir / "events.jsonl")
    log.emit("dispatch", node_id="a", attempt=1, route_id="cc/x")
    log.emit("dispatch", node_id="a", attempt=2, route_id="cx/y")
    assert prior_attempts(run_dir) == {"a": 2}


class _ScriptedExec:
    """Writes the node's owned file; optionally 'dies' on one node (controller loss)."""

    def __init__(self, die_on: str | None = None) -> None:
        self.die_on = die_on
        self.calls: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        # run_golden_path hydrates the real prompt: "GOAL: ...\n\nNODE: <id>\n..."
        node_id = next(
            line.split(":", 1)[1].strip()
            for line in prompt.splitlines()
            if line.startswith("NODE:")
        )
        self.calls.append(node_id)
        if node_id == self.die_on:
            # The generation's only route is exhausted: this controller life ends.
            return WorkerTerminal(ok=False, model=route_id, status_code=429, error="usage limit")
        (cwd / f"{node_id}.txt").write_text(f"{node_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


async def test_new_controller_generation_resumes_without_repeating_validated_work(
    tmp_path: Path,
) -> None:
    """BOD-264 AC5: after a controller generation ends mid-run, the next generation
    resumes the SAME durable run and never re-executes a validated node."""

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

    def graph() -> WorkGraph:
        return WorkGraph(
            "g",
            (
                WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),
                WorkNode(
                    "b",
                    "write b",
                    depends_on=("a",),
                    owned_files=("b.txt",),
                    verification_command=("true",),
                ),
            ),
        )

    runs = tmp_path / "runs"
    common: dict[str, Any] = {
        "repo": repo,
        "runs_root": runs,
        "selector": _Sel(),
        "classifier": _Classifier(),
        "reviewer": _Reviewer(),
        "run_id": "r1",
    }
    from verdict.orchestration.runtime import RuntimePolicy

    # Generation 0 gets one attempt per node, then its life ends BLOCKED.
    # (Attempt numbers continue across lives, so its attempts count against the
    # node's total budget; generation 1 uses the default budget.)
    gen0 = _ScriptedExec(die_on="b")
    first = await run_golden_path(
        "g", executor=gen0, graph=graph(), policy=RuntimePolicy(max_attempts_per_node=1), **common
    )
    assert first.outcome == "BLOCKED"
    assert gen0.calls == ["a", "b"]
    assert set(prior_validated(runs / "r1")) == {"a"}

    gen1 = _ScriptedExec()
    result = await run_golden_path("g", executor=gen1, graph=graph(), **common)

    assert result.outcome == "COMPLETE", result.reason
    # The validated node from generation 0 is reused, never re-executed.
    assert gen1.calls == ["b"]
    events = list(EventLog(runs / "r1" / "events.jsonl").read())
    resumed = [e for e in events if e.type == "controller" and e.data.get("state") == "RESUMED"]
    assert resumed and "1 validated node" in str(resumed[-1].data.get("detail"))
    dispatched_a = [e for e in events if e.type == "dispatch" and e.node_id == "a"]
    assert len(dispatched_a) == 1


async def test_resume_preserves_original_producer_snapshot(tmp_path: Path) -> None:
    """BOD-225: resume keeps the first run_started producer even if git/package change."""
    import json
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

    def graph() -> WorkGraph:
        return WorkGraph(
            "g",
            (
                WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),
                WorkNode(
                    "b",
                    "write b",
                    depends_on=("a",),
                    owned_files=("b.txt",),
                    verification_command=("true",),
                ),
            ),
        )

    runs = tmp_path / "runs"
    common: dict[str, Any] = {
        "repo": repo,
        "runs_root": runs,
        "selector": _Sel(),
        "classifier": _Classifier(),
        "reviewer": _Reviewer(),
        "run_id": "r1",
    }
    from verdict.orchestration.runtime import RuntimePolicy

    gen0 = _ScriptedExec(die_on="b")
    first = await run_golden_path(
        "g", executor=gen0, graph=graph(), policy=RuntimePolicy(max_attempts_per_node=1), **common
    )
    assert first.outcome == "BLOCKED"

    events = list(EventLog(runs / "r1" / "events.jsonl").read())
    started = [e for e in events if e.type == "run_started"]
    assert started, "first life must emit run_started"
    original = started[0].data.get("producer")
    assert isinstance(original, dict)
    assert set(original) == {"verdict_version", "git_sha", "dirty"}
    assert original["git_sha"]
    assert original["dirty"] is False

    # Mutate repository state before resume: new commit + dirty working tree.
    (repo / "extra.txt").write_text("new\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "later"], cwd=repo, check=True)
    (repo / "untracked.txt").write_text("dirty\n")
    current = capture_producer(repo=repo)
    assert current["git_sha"] != original["git_sha"]
    assert current["dirty"] is True

    gen1 = _ScriptedExec()
    result = await run_golden_path("g", executor=gen1, graph=graph(), **common)
    assert result.outcome == "COMPLETE", result.reason

    events = list(EventLog(runs / "r1" / "events.jsonl").read())
    started = [e for e in events if e.type == "run_started"]
    assert len(started) >= 2
    # Every resumed run_started must carry the original producer snapshot.
    for event in started:
        assert event.data.get("producer") == original

    receipt = build_run_receipt(runs / "r1")
    assert receipt["producer"] == original
    # Real receipt builder, not a hand-built dict.
    assert json.loads((runs / "r1" / "events.jsonl").read_text().splitlines()[0])


async def test_real_run_records_producer_on_first_start(tmp_path: Path) -> None:
    """A newly started real orchestration run records the three-key producer object."""
    import subprocess

    import verdict

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
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=tmp_path / "runs",
        selector=_Sel(),
        executor=_ScriptedExec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        graph=graph,
        run_id="fresh",
    )
    assert result.outcome == "COMPLETE", result.reason
    events = list(EventLog(tmp_path / "runs" / "fresh" / "events.jsonl").read())
    started = next(e for e in events if e.type == "run_started")
    producer = started.data["producer"]
    assert set(producer) == {"verdict_version", "git_sha", "dirty"}
    assert producer["verdict_version"] == verdict.__version__
    assert producer["git_sha"] == head
    assert producer["dirty"] is False
    receipt = build_run_receipt(tmp_path / "runs" / "fresh")
    assert receipt["producer"] == producer
