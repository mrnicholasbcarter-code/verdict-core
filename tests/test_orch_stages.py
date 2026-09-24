"""BOD-188: UNDERSTAND and HYDRATE stages in the orchestration run view."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
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
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.tui import RunView, event_line, render_text

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def event(seq: int, kind: str, node: str = "", **data: object) -> Any:
    from verdict.orchestration.contracts import RunEvent

    return RunEvent(
        seq=seq, at=f"2026-01-01T00:00:{seq:02d}+00:00", type=kind, node_id=node, data=data
    )


# ---------------------------------------------------------------- runtime: hydrate


class _Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> Any:
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["type"] == type and (node_id is None or r["node_id"] == node_id)
        ]


class _Selector:
    def evaluate(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict, ...]:
        return self.select(requirements, now=now)[1]

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        v = RouteVerdict(
            "cc/x", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION, rank=0
        )
        return v, (v,)

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None: ...

    def record_success(self, route_id: str, *, now: datetime) -> None: ...


class _Classifier:
    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification:
        return FailureClassification("unknown", "REROUTE", 1, "route")


class _Executor:
    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        (cwd / "a.txt").write_text("a written\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)


class _Reviewer:
    async def review(self, **kwargs: Any) -> ReviewResult:
        return ReviewResult("PASS", "fake", "cx/rev")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n")
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


async def test_runtime_emits_hydrate_per_attempt_with_correct_byte_count(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    node = WorkNode(
        "a",
        "write a",
        owned_files=("a.txt",),
        required_context=("README.md",),
        verification_command=("true",),
    )
    graph = WorkGraph("g", (node,))
    events = _Events()
    expected_prompt = "GOAL: g\nNODE: a"

    def prompt_for(n: WorkNode, cwd: Path) -> str:
        return expected_prompt

    runtime = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run1",
        graph=graph,
        selector=_Selector(),
        executor=_Executor(),
        classifier=_Classifier(),
        events=events,
        prompt_for=prompt_for,
        reviewer=_Reviewer(),
        policy=RuntimePolicy(require_review=False),
        now=lambda: NOW,
    )
    result = await runtime.run()
    assert result.outcome.value == "COMPLETE", result.reason
    hydrate_events = events.of("hydrate", "a")
    assert len(hydrate_events) == 1
    hydrate = hydrate_events[0]
    assert hydrate["context_files"] == ["README.md"]
    assert hydrate["prompt_bytes"] == len(expected_prompt.encode())
    assert hydrate["truncated"] is False
    assert hydrate["budget_bytes"] == 60_000


async def test_runtime_hydrate_marks_truncated_prompt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    node = WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",))
    graph = WorkGraph("g", (node,))
    events = _Events()
    truncated_prompt = "some context\n[TRUNCATED: 10 bytes omitted]\n"

    runtime = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run1",
        graph=graph,
        selector=_Selector(),
        executor=_Executor(),
        classifier=_Classifier(),
        events=events,
        prompt_for=lambda n, cwd: truncated_prompt,
        reviewer=_Reviewer(),
        policy=RuntimePolicy(require_review=False),
        now=lambda: NOW,
    )
    await runtime.run()
    hydrate = events.of("hydrate", "a")[0]
    assert hydrate["truncated"] is True
    assert hydrate["prompt_bytes"] == len(truncated_prompt.encode())


# ---------------------------------------------------------------- run.py: understand


async def test_run_golden_path_emits_understand_before_plan_started(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runs = tmp_path / "runs"
    node = WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",))
    graph = WorkGraph("g", (node,))

    result = await run_golden_path(
        "ship the widget",
        repo=repo,
        runs_root=runs,
        selector=_Selector(),
        executor=_Executor(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        graph=graph,
        run_id="r1",
    )
    assert result.outcome == "COMPLETE", result.reason
    events_path = runs / "r1" / "events.jsonl"
    from verdict.orchestration.tui import read_events

    events = read_events(events_path)
    types = [e.type for e in events]
    assert "understand" in types
    understand_idx = types.index("understand")
    # No plan_started is emitted at all here because a graph was supplied directly
    # (planning is skipped), but understand must still be emitted before plan_ready
    # and before any node dispatch/selection.
    assert understand_idx < types.index("plan_ready")
    understand = events[understand_idx].data
    assert understand["goal_chars"] == len("ship the widget")
    assert understand["scope"] == f"repo: {repo.name}"
    assert understand["proof_requirements"] == [
        "node verification",
        "integration barrier",
        "independent review",
    ]
    # a supplied graph is known at emit time, so risk reflects the node's risk
    assert understand["risk"] == "low"


async def test_understand_risk_is_unknown_without_a_graph(tmp_path: Path) -> None:
    """When planning happens after understand, risk is unknown until the plan exists."""
    from verdict.orchestration.run import _task_profile

    profile = _task_profile("a goal", tmp_path, None)
    assert profile["risk"] == "unknown"
    assert profile["goal_chars"] == len("a goal")
    assert profile["scope"] == f"repo: {tmp_path.name}"


def test_understand_risk_is_max_of_graph_nodes() -> None:
    from verdict.orchestration.run import _task_profile

    graph = WorkGraph(
        "g",
        (
            WorkNode("a", "a", risk="low", owned_files=("a.txt",), verification_command=("true",)),
            WorkNode("b", "b", risk="high", owned_files=("b.txt",), verification_command=("true",)),
        ),
    )
    profile = _task_profile("g", Path("/tmp/myrepo"), graph)
    assert profile["risk"] == "high"


# ---------------------------------------------------------------- TUI rendering


def test_tui_renders_understand_and_hydrate_wide_mode() -> None:
    events = [
        event(1, "run_started", goal="ship it"),
        event(
            2,
            "understand",
            goal_chars=8,
            risk="low",
            proof_requirements=["node verification", "integration barrier", "independent review"],
            scope="repo: verdict-core",
        ),
        event(3, "plan_started", route_id="cc/planner"),
        event(
            4,
            "plan_ready",
            nodes=[{"node_id": "N1", "objective": "build"}],
            layers=[["N1"]],
            topology="SOLO",
            rationale=["bounded"],
        ),
        event(5, "selection", "N1", route_id="cc/claude-sonnet-5", capacity_class="subscription"),
        event(
            6,
            "hydrate",
            "N1",
            context_files=["docs/adr/ADR-036.md"],
            prompt_bytes=2048,
            truncated=False,
            budget_bytes=60000,
        ),
        event(7, "node_state", "N1", state="VALIDATED"),
        event(8, "run_finished", outcome="COMPLETE", reason="ok"),
    ]
    view = RunView.from_events(events)
    assert view.understand == {
        "goal_chars": 8,
        "risk": "low",
        "proof_requirements": ["node verification", "integration barrier", "independent review"],
        "scope": "repo: verdict-core",
    }
    node = view.nodes["N1"]
    assert node.context_files == 1
    assert node.prompt_bytes == 2048
    assert node.truncated is False

    text = render_text(events, width=140, plain=False)
    assert "UNDERSTAND" in text
    assert "HYDRATE" in text
    assert "risk: low" in text
    assert "N1" in text and "2.0KB" in text


def test_tui_renders_understand_and_hydrate_plain_mode() -> None:
    events = [
        event(1, "run_started", goal="ship it"),
        event(
            2,
            "understand",
            goal_chars=8,
            risk="medium",
            proof_requirements=["node verification"],
            scope="repo: verdict-core",
        ),
        event(
            3,
            "hydrate",
            "N1",
            context_files=["a.md", "b.md"],
            prompt_bytes=1024,
            truncated=True,
            budget_bytes=60000,
        ),
        event(4, "run_finished", outcome="BLOCKED", reason="stopped"),
    ]
    text = render_text(events, width=100, plain=True)
    assert "\x1b" not in text
    assert "UNDERSTAND" in text
    assert "HYDRATE" in text
    assert "risk: medium" in text
    assert "1.0KB" in text
    assert "truncated" in text.lower()


def test_understand_event_line_has_stage_tag() -> None:
    line = event_line(
        event(
            1,
            "understand",
            goal_chars=10,
            risk="low",
            proof_requirements=["node verification"],
            scope="repo: verdict-core",
        )
    )
    assert line.startswith("[UNDERSTAND]")
    assert "risk=low" in line
    assert "goal_chars=10" in line


def test_hydrate_event_line_has_stage_tag() -> None:
    line = event_line(
        event(
            1,
            "hydrate",
            "N1",
            context_files=["a.md"],
            prompt_bytes=2048,
            truncated=True,
            budget_bytes=60000,
        )
    )
    assert line.startswith("[HYDRATE]")
    assert "N1" in line
    assert "2.0KB" in line
    assert "truncated" in line.lower()


def test_every_event_type_has_narration_including_new_stages() -> None:
    for seq, kind in enumerate(("understand", "hydrate"), 1):
        line = event_line(
            event(
                seq,
                kind,
                "N1",
                goal_chars=5,
                risk="low",
                proof_requirements=["node verification"],
                scope="repo: x",
                context_files=["a.md"],
                prompt_bytes=100,
                truncated=False,
                budget_bytes=60000,
            )
        )
        assert line.startswith("[") and "]" in line and len(line) > 5, kind


async def test_run_golden_path_emits_understand_before_plan_started_when_planning(
    tmp_path: Path,
) -> None:
    """When no graph is supplied, planning runs and emits plan_started; understand precedes it."""
    repo = _repo(tmp_path)
    runs = tmp_path / "runs"

    plan_json = (
        '{"nodes": [{"node_id": "a", "objective": "write a", "kind": "implement", '
        '"owned_files": ["a.txt"], "verification_command": ["true"]}, '
        '{"node_id": "z", "objective": "integrate", "kind": "integrate", '
        '"depends_on": ["a"], "verification_command": ["true"]}]}'
    )

    class _PlanningExecutor:
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            if "OUTPUT CONTRACT" in prompt:
                return WorkerTerminal(ok=True, output=plan_json, model=route_id)
            (cwd / "a.txt").write_text("a written\n")
            return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)

    result = await run_golden_path(
        "ship the widget",
        repo=repo,
        runs_root=runs,
        selector=_Selector(),
        executor=_PlanningExecutor(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        run_id="r2",
    )
    assert result.outcome == "COMPLETE", result.reason
    from verdict.orchestration.tui import read_events

    events = read_events(runs / "r2" / "events.jsonl")
    types = [e.type for e in events]
    assert "understand" in types and "plan_started" in types
    assert types.index("understand") < types.index("plan_started")


async def test_run_golden_path_skips_eligibility_when_summary_all_zeros(tmp_path: Path) -> None:
    """Verify run_golden_path does not emit eligibility event when summary returns all zeros."""
    repo = _repo(tmp_path)
    runs = tmp_path / "runs"
    node = WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",))
    graph = WorkGraph("g", (node,))

    result = await run_golden_path(
        "ship the widget",
        repo=repo,
        runs_root=runs,
        selector=_Selector(),
        executor=_Executor(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        graph=graph,
        run_id="r_zero",
        summary=lambda: {
            "discovered": 0,
            "entitled": 0,
            "healthy": 0,
            "available": 0,
            "eligible": 0,
        },
    )
    assert result.outcome == "COMPLETE", result.reason
    events_path = runs / "r_zero" / "events.jsonl"
    from verdict.orchestration.tui import read_events

    events = read_events(events_path)
    # Should NOT have any eligibility event with empty node_id when all counts are zero
    eligibility_events = [e for e in events if e.type == "eligibility" and e.node_id == ""]
    assert len(eligibility_events) == 0, (
        f"Should not emit eligibility event when all counts are zero, "
        f"but found {len(eligibility_events)} event(s)"
    )


async def test_run_golden_path_emits_eligibility_when_summary_nonzero(tmp_path: Path) -> None:
    """Verify run_golden_path emits eligibility event when summary has nonzero counts."""
    repo = _repo(tmp_path)
    runs = tmp_path / "runs"
    node = WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",))
    graph = WorkGraph("g", (node,))

    result = await run_golden_path(
        "ship the widget",
        repo=repo,
        runs_root=runs,
        selector=_Selector(),
        executor=_Executor(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        graph=graph,
        run_id="r_nonzero",
        summary=lambda: {
            "discovered": 9,
            "entitled": 4,
            "healthy": 3,
            "available": 2,
            "eligible": 1,
        },
    )
    assert result.outcome == "COMPLETE", result.reason
    events_path = runs / "r_nonzero" / "events.jsonl"
    from verdict.orchestration.tui import read_events

    events = read_events(events_path)
    # Should have exactly one eligibility event with empty node_id
    eligibility_events = [e for e in events if e.type == "eligibility" and e.node_id == ""]
    assert len(eligibility_events) == 1, (
        f"Should emit exactly one eligibility event when counts are nonzero, "
        f"but found {len(eligibility_events)} event(s)"
    )
    # Verify the data includes the expected discovered count
    assert eligibility_events[0].data["discovered"] == 9, (
        f"Expected discovered=9, got {eligibility_events[0].data['discovered']}"
    )
