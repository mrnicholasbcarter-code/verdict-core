"""Runtime integration: research pack persistence and reuse across attempts.

Exercises the runtime's per-node ResearchPack persistence (create on first
attempt, reuse-and-append-delta on retry/replacement) together with the
runner-side hydration that passes a prepared context so the worker does not
repeat settled research. Fixture-only; no live provider calls.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    RouteVerdict,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.planner import hydrate_node_prompt
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.worker_context import (
    ResearchDelta,
    ResearchPack,
    WorkerContext,
    WorkerPrompt,
    build_worker_context,
    create_research_pack,
    project_research_pack,
    reuse_research_pack,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg/widget.py").write_text("def compute_widget(value):\n    return value + 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_widget.py").write_text("def test_x():\n    assert True\n")
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "fixture"],
        ["git", "config", "user.email", "f@example.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    return repo


class Selector:
    """Always returns the same route, ignoring exclusions (enables retry)."""

    def select(self, requirements, *, now):
        route = RouteVerdict(
            "cc/s", "cc", EligibilityStage.SELECTED, None, "fixture", CapacityClass.FREE
        )
        return route, (route,)

    def evaluate(self, requirements, *, now):
        return self.select(requirements, now=now)[1]

    def record_success(self, *args, **kwargs):
        pass

    def record_failure(self, *args, **kwargs):
        pass


class Events:
    def emit(self, *args, **kwargs):
        pass


class Classifier:
    def classify(self, terminal, *, now):
        return FailureClassification("retryable", "REROUTE", 0, "route", terminal.error or "retry")


class Executor:
    """Fails the first call, succeeds on the second; writes nothing on success."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, prompt, *, route_id, cwd, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return WorkerTerminal(
                ok=False,
                model=route_id,
                status_code=429,
                error="usage limit",
                executor_kind="scripted",
            )
        return WorkerTerminal(
            ok=True, output="RESULT: DONE", model=route_id, executor_kind="scripted"
        )


def _prompt_factory(
    repo: Path,
    run_dir: Path,
    goal: str,
    build_calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
):
    """Build a prompt_for that loads/reuses a persisted ResearchPack.

    Mirrors verdict/orchestration/run.py's hydrate_with_research_pack so the
    runtime integration is exercised end to end. build_worker_context is
    wrapped to count retrieval calls: a reused pack must not call it again.
    """
    original_build = build_worker_context

    def counting_build(
        node: WorkNode,
        *,
        repo: Path,
        max_context_bytes: int,
        context_plan: ContextPlan | None = None,
        mandatory_prompt_tokens: int = 0,
    ) -> WorkerContext:
        build_calls["n"] += 1
        return original_build(
            node,
            repo=repo,
            max_context_bytes=max_context_bytes,
            context_plan=context_plan,
            mandatory_prompt_tokens=mandatory_prompt_tokens,
        )

    monkeypatch.setattr("verdict.orchestration.planner.build_worker_context", counting_build)

    def prompt_for(
        node: WorkNode,
        cwd: Path,
        *,
        context_budget_bytes: int = 60_000,
        context_plan: ContextPlan | None = None,
        mandatory_suffix: str = "",
    ) -> WorkerPrompt:
        pack_dir = run_dir / "context_artifacts"
        pack_path = pack_dir / f"{node.node_id}.json"
        prepared: WorkerContext | None = None
        if pack_path.exists():
            try:
                research = ResearchPack.from_dict(json.loads(pack_path.read_text()))
                repo_revision = subprocess.run(
                    ["git", "-C", str(cwd), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout.strip()
                research = reuse_research_pack(
                    node,
                    research,
                    repo_revision=repo_revision,
                    source_revisions=dict(research.source_revisions),
                )
                prepared = project_research_pack(
                    research,
                    max_context_bytes=context_budget_bytes,
                    context_plan=context_plan,
                    mandatory_prompt_tokens=estimate_tokens(mandatory_suffix) + 1,
                )
            except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError):
                # Stale pack or unreadable worktree: build fresh context.
                prepared = None
        return hydrate_node_prompt(
            node,
            repo=cwd,
            goal=goal,
            max_context_bytes=context_budget_bytes,
            max_prompt_bytes=context_budget_bytes,
            context_plan=context_plan,
            mandatory_suffix=mandatory_suffix,
            structured=True,
            prepared_context=prepared,
        )

    return prompt_for


async def _run(
    tmp_path: Path,
    node: WorkNode,
    executor: Executor,
    build_calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    goal: str = "ship widget",
) -> Path:
    repo = _make_repo(tmp_path)
    run_dir = tmp_path / "run"
    prompt_for = _prompt_factory(repo, run_dir, goal, build_calls, monkeypatch)
    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=WorkGraph(goal, (node,)),
        selector=Selector(),
        executor=executor,
        classifier=Classifier(),
        events=Events(),
        prompt_for=prompt_for,
        policy=RuntimePolicy(require_review=False, run_repo_gates=False),
        now=lambda: NOW,
    )
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    return run_dir


def _persisted_pack(run_dir: Path, node_id: str) -> dict[str, Any]:
    pack_path = run_dir / "context_artifacts" / f"{node_id}.json"
    assert pack_path.is_file(), f"no persisted research pack at {pack_path}"
    return json.loads(pack_path.read_text())


async def test_research_pack_persisted_and_reused_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First attempt fails, second reuses the pack (zero retrieval calls)."""
    node = WorkNode(
        "widget",
        "Update compute_widget",
        owned_files=("pkg/widget.py",),
        verification_command=("true",),
        acceptance=("integer contract",),
    )
    executor = Executor()
    build_calls: dict[str, int] = {"n": 0}
    run_dir = await _run(tmp_path, node, executor, build_calls, monkeypatch)

    assert executor.calls == 2
    # Two attempts: first built fresh context, second reused the pack.
    assert build_calls["n"] == 1, f"expected exactly one retrieval, got {build_calls['n']}"

    pack = _persisted_pack(run_dir, "widget")
    research_pack_id = pack["research_pack_id"]
    assert research_pack_id
    # A failure delta was appended on the first (failed) attempt.
    deltas = pack["deltas"]
    assert len(deltas) == 1
    assert deltas[0]["kind"] == "failure"
    assert deltas[0]["reference"] == "attempt-1"
    # Re-hydrating against the persisted pack reuses the same id (no rebuild).
    reused_context = project_research_pack(
        ResearchPack.from_dict(
            json.loads((run_dir / "context_artifacts" / "widget.json").read_text())
        ),
        max_context_bytes=60_000,
        mandatory_prompt_tokens=1,
    )
    assert reused_context.research_pack_id == research_pack_id
    assert reused_context.research_deltas == tuple(ResearchDelta(**d) for d in deltas)


async def test_research_pack_created_on_first_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful first attempt persists a ResearchPack with a stable id."""
    node = WorkNode(
        "widget",
        "Update compute_widget",
        owned_files=("pkg/widget.py",),
        verification_command=("true",),
        acceptance=("integer contract",),
    )

    class SucceedExecutor(Executor):
        async def run(self, prompt, *, route_id, cwd, timeout_seconds):
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, executor_kind="scripted"
            )

    build_calls: dict[str, int] = {"n": 0}
    run_dir = await _run(tmp_path, node, SucceedExecutor(), build_calls, monkeypatch)

    # Single successful attempt: fresh build only.
    assert build_calls["n"] == 1
    pack = _persisted_pack(run_dir, "widget")
    assert pack["research_pack_id"]
    assert pack["task_digest"]
    assert pack["repo_revision"]
    assert pack["research_revision"]
    # No deltas on a clean success.
    assert pack["deltas"] == []


async def test_stale_pack_refused_and_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale persisted pack (different task digest) is refused and rebuilt."""
    repo = _make_repo(tmp_path)
    run_dir = tmp_path / "run"

    # Pre-persist a pack built for a *different* task digest (old objective).
    old_node = WorkNode(
        "widget",
        "Update compute_widget v1",
        owned_files=("pkg/widget.py",),
        verification_command=("true",),
    )
    context = build_worker_context(old_node, repo=repo)
    stale_pack = create_research_pack(old_node, context, repo_revision="HEAD", source_revisions={})
    pack_dir = run_dir / "context_artifacts"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "widget.json").write_text(json.dumps(stale_pack.to_dict(), indent=2))

    node = WorkNode(
        "widget",
        "Update compute_widget v2",
        owned_files=("pkg/widget.py",),
        verification_command=("true",),
        acceptance=("integer contract",),
    )

    class SucceedExecutor(Executor):
        async def run(self, prompt, *, route_id, cwd, timeout_seconds):
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, executor_kind="scripted"
            )

    build_calls: dict[str, int] = {"n": 0}
    prompt_for = _prompt_factory(repo, run_dir, "ship widget", build_calls, monkeypatch)
    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=WorkGraph("ship widget", (node,)),
        selector=Selector(),
        executor=SucceedExecutor(),
        classifier=Classifier(),
        events=Events(),
        prompt_for=prompt_for,
        policy=RuntimePolicy(require_review=False, run_repo_gates=False),
        now=lambda: NOW,
    )
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason

    # Stale pack refused -> build_worker_context called for a fresh build.
    assert build_calls["n"] == 1
    pack = _persisted_pack(run_dir, "widget")
    assert pack["research_pack_id"] != stale_pack.research_pack_id
    assert pack["task_digest"] != stale_pack.task_digest
