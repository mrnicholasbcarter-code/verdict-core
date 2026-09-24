"""DagRuntime: parallel fan-out, same-node reassignment, isolation, barriers, review."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    NodeKind,
    NodeState,
    ReviewFinding,
    ReviewResult,
    RouteVerdict,
    RunOutcome,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> None:
        json.dumps(data, default=str)
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["type"] == type and (node_id is None or r["node_id"] == node_id)
        ]


class Selector:
    """Pool with cooldowns; mirrors the ModelSelector contract."""

    def __init__(self, routes: list[str]) -> None:
        self.routes = routes
        self.cool: dict[str, float] = {}
        self.successes: list[str] = []

    def evaluate(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict, ...]:
        return self.select(requirements, now=now)[1]

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        verdicts = []
        chosen = None
        for rank, route in enumerate(self.routes, 1):
            provider = route.split("/")[0]
            if route in requirements.exclude_routes:
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.AVAILABLE,
                        EligibilityStage.TASK_ELIGIBLE,
                        "excluded",
                    )
                )
                continue
            if (
                self.cool.get(route, 0) > now.timestamp()
                or self.cool.get(provider, 0) > now.timestamp()
            ):
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.HEALTHY,
                        EligibilityStage.AVAILABLE,
                        "cooldown",
                    )
                )
                continue
            v = RouteVerdict(
                route,
                provider,
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.SUBSCRIPTION,
                rank=rank,
            )
            verdicts.append(v)
            if chosen is None:
                chosen = v
        return chosen, tuple(verdicts)

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        key = route_id.split("/")[0] if failure.scope == "provider" else route_id
        self.cool[key] = now.timestamp() + failure.cooldown_seconds

    def record_success(self, route_id: str, *, now: datetime) -> None:
        self.successes.append(route_id)


class Classifier:
    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification:
        if terminal.status_code == 429:
            return FailureClassification(
                "quota_exhausted", "REROUTE", 3600, "provider", terminal.error
            )
        if terminal.error.startswith("verification_failed"):
            return FailureClassification(
                "verification_failed", "CORRECT_IMPLEMENTATION", 0, "none", terminal.error
            )
        if terminal.error.startswith("ownership_violation"):
            return FailureClassification(
                "ownership_violation", "REHYDRATE", 0, "none", terminal.error
            )
        return FailureClassification("unknown", "REROUTE", 120, "route", terminal.error)


class Executor:
    """Writes the owned file per a behaviour table keyed by (node, route)."""

    def __init__(self, behaviour: dict[tuple[str, str], str], delay: float = 0.05) -> None:
        self.behaviour = behaviour
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        node = prompt.split(":", 1)[0]
        self.calls.append((node, route_id))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            kind = self.behaviour.get((node, route_id), self.behaviour.get((node, "*"), "ok"))
            if kind == "quota":
                return WorkerTerminal(
                    ok=False, model=route_id, status_code=429, error="usage limit"
                )
            if kind == "hang":
                await asyncio.sleep(timeout_seconds + 60)
            if kind == "crash":
                raise RuntimeError("executor exploded")
            if kind == "outside":
                (cwd / "not_owned.txt").write_text("x")
                return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)
            if kind == "badcode":
                (cwd / f"{node}.txt").write_text("FAIL")
                return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)
            (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
            )
        finally:
            self.active -= 1


class Reviewer:
    def __init__(self, status: str = "PASS", blocking: bool = False) -> None:
        self.status, self.blocking = status, blocking
        self.calls: list[dict[str, Any]] = []

    async def review(self, **kwargs: Any) -> ReviewResult:
        self.calls.append(kwargs)
        findings = (ReviewFinding("high", "bug", "a.txt", 1, "bad"),) if self.blocking else ()
        return ReviewResult(self.status, "fake-ocr", "cx/reviewer", findings)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def node(nid: str, deps: tuple[str, ...] = (), kind: NodeKind = NodeKind.IMPLEMENT) -> WorkNode:
    check = ["sh", "-c", f"test -f {nid}.txt && ! grep -q FAIL {nid}.txt"]
    return WorkNode(
        nid,
        f"write {nid}",
        kind=kind,
        depends_on=deps,
        owned_files=(f"{nid}.txt",),
        verification_command=tuple(check),
    )


def make(
    repo: Path,
    graph: WorkGraph,
    executor: Executor,
    routes: list[str],
    reviewer: Reviewer | None = None,
    **policy: Any,
) -> tuple[DagRuntime, Events, Selector]:
    events, selector = Events(), Selector(routes)
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=graph,
        selector=selector,
        executor=executor,
        classifier=Classifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=reviewer if reviewer is not None else Reviewer(),
        policy=RuntimePolicy(**policy),
        now=lambda: NOW,
    )
    return runtime, events, selector


async def test_independent_nodes_run_concurrently_and_complete(repo: Path) -> None:
    graph = WorkGraph(
        "g", (node("a"), node("b"), node("c"), node("d", ("a", "b", "c"))), max_parallel=3
    )
    ex = Executor({}, delay=0.3)
    rt, ev, _ = make(repo, graph, ex, ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.max_active == 3
    assert all(r.state is NodeState.VALIDATED for r in result.nodes.values())
    # d was built on top of a, b and c
    out = subprocess.run(
        ["git", "show", f"{result.integration_ref}:a.txt"], cwd=repo, capture_output=True, text=True
    )
    assert out.returncode == 0
    assert ev.of("run_finished")[0]["outcome"] == "COMPLETE"


async def test_quota_failure_reassigns_same_node_to_other_provider(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"), node("b")))
    ex = Executor({("a", "cc/s"): "quota"})
    rt, ev, _sel = make(repo, graph, ex, ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    reassign = ev.of("reassign", "a")
    assert reassign and reassign[0]["from_route"] == "cc/s" and reassign[0]["to_route"] == "cx/g"
    assert ev.of("cooldown", "a")[0]["scope"] == "provider"
    assert result.nodes["a"].history[0]["outcome"] == "quota_exhausted"
    # prompt (contract) was identical across attempts
    assert [c for c in ex.calls if c[0] == "a"] == [("a", "cc/s"), ("a", "cx/g")]


async def test_failed_node_does_not_cancel_healthy_sibling(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"), node("b"), node("c", ("a",))))
    ex = Executor({("a", "*"): "quota"})
    rt, _ev, _ = make(repo, graph, ex, ["cc/s", "cx/g"], max_attempts_per_node=2)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert result.nodes["b"].state is NodeState.VALIDATED
    assert result.nodes["a"].state is NodeState.BLOCKED
    assert result.nodes["c"].state is NodeState.BLOCKED
    assert "dependency blocked" in result.nodes["c"].reason


async def test_pool_exhaustion_fails_closed_explicitly(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    ex = Executor({("a", "*"): "quota"})
    rt, ev, _ = make(repo, graph, ex, ["cc/s", "cc/o"])
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    fails = ev.of("failure", "a")
    assert fails[-1]["action"] == "FAIL_CLOSED" and fails[-1]["category"] == "pool_exhausted"


async def test_executor_crash_is_isolated_and_reassigned(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    ex = Executor({("a", "cc/s"): "crash"})
    rt, ev, _ = make(repo, graph, ex, ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE
    assert "transport" in ev.of("failure", "a")[0]["evidence"]


async def test_hung_worker_is_timed_out_and_reassigned(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    ex = Executor({("a", "cc/s"): "hang"})
    rt, ev, _ = make(repo, graph, ex, ["cc/s", "cx/g"], attempt_timeout_seconds=0.2)
    rt.policy = RuntimePolicy(attempt_timeout_seconds=0.2)
    # watchdog = attempt timeout + 30s; shrink for the test
    import verdict.orchestration.runtime as mod

    orig = mod.asyncio.wait_for

    async def fast_wait_for(aw: Any, timeout: float | None) -> Any:
        return await orig(aw, 0.3 if timeout and timeout > 1 else timeout)

    mod.asyncio.wait_for = fast_wait_for  # type: ignore[assignment]
    try:
        result = await rt.run()
    finally:
        mod.asyncio.wait_for = orig  # type: ignore[assignment]
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert "timeout" in ev.of("failure", "a")[0]["evidence"]


async def test_ownership_violation_rejected_and_reassigned(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    ex = Executor({("a", "cc/s"): "outside"})
    rt, ev, _ = make(repo, graph, ex, ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE
    barrier = ev.of("barrier", "a")[0]
    assert barrier["name"] == "ownership" and barrier["ok"] is False
    assert any(r["state"] == "REJECTED" for r in ev.of("node_state", "a"))


async def test_verification_failure_is_rejected_not_validated(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    ex = Executor({("a", "cc/s"): "badcode"})
    rt, ev, _ = make(repo, graph, ex, ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE
    verifies = ev.of("verify", "a")
    assert verifies[0]["ok"] is False and verifies[-1]["ok"] is True


async def test_admission_is_not_success_lifecycle_order(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    rt, ev, _ = make(repo, graph, Executor({}), ["cc/s"])
    await rt.run()
    states = [r["state"] for r in ev.of("node_state", "a")]
    assert states == ["ADMITTED", "DISPATCHED", "RUNNING", "TERMINAL_SUCCESS", "VALIDATED"]


async def test_review_failure_blocks_completion(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    rt, _ev, _ = make(repo, graph, Executor({}), ["cc/s"], reviewer=Reviewer("FAIL", blocking=True))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED and "review" in result.reason


async def test_review_error_fails_closed(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    rt, _ev, _ = make(repo, graph, Executor({}), ["cc/s"], reviewer=Reviewer("ERROR"))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED


async def test_reviewer_excludes_implementer_routes(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"), node("b")))
    reviewer = Reviewer()
    rt, _, _ = make(repo, graph, Executor({}), ["cc/s", "cx/g"], reviewer=reviewer)
    await rt.run()
    assert "cc/s" in reviewer.calls[0]["exclude_routes"]


async def test_worker_reported_blocked_is_a_failure(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))

    class Blocked(Executor):
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            if route_id == "cc/s":
                return WorkerTerminal(ok=True, output="RESULT: BLOCKED cannot", model=route_id)
            return await super().run(
                prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
            )

    rt, ev, _ = make(repo, graph, Blocked({}), ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE
    assert "worker_blocked" in ev.of("failure", "a")[0]["evidence"]


async def test_no_eligible_model_blocks_with_reason(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"),))
    rt, _ev, sel = make(repo, graph, Executor({}), ["cc/s"])
    sel.cool["cc"] = (NOW + timedelta(hours=1)).timestamp()
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED and "no eligible model" in result.reason


async def test_concurrent_selection_spreads_load_across_routes(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"), node("b"), node("c")), max_parallel=3)
    inflight: dict[str, str] = {}

    class Spreading(Selector):
        def select(
            self, requirements: TaskRequirements, *, now: datetime
        ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
            load = {r: sum(1 for v in inflight.values() if v == r) for r in self.routes}
            ordered = sorted(self.routes, key=lambda r: (load[r], self.routes.index(r)))
            original, self.routes = self.routes, ordered
            try:
                return super().select(requirements, now=now)
            finally:
                self.routes = original

    events = Events()
    ex = Executor({}, delay=0.2)
    rt = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=graph,
        selector=Spreading(["cc/s", "cc/o", "cx/g"]),
        executor=ex,
        classifier=Classifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        now=lambda: NOW,
        inflight=inflight,
    )
    await rt.run()
    assert {r for _, r in ex.calls} == {"cc/s", "cc/o", "cx/g"}


async def test_worktrees_are_cleaned_after_success(repo: Path) -> None:
    integrate = WorkNode(
        "i",
        "integrate",
        kind=NodeKind.INTEGRATE,
        depends_on=("a", "b"),
        verification_command=("sh", "-c", "test -f a.txt && test -f b.txt"),
    )
    graph = WorkGraph("g", (node("a"), node("b"), integrate))
    rt, _, _ = make(repo, graph, Executor({}), ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert len(listed.strip().splitlines()) == 1


async def test_already_satisfied_node_validates_when_check_passes(repo: Path) -> None:
    (repo / "a.txt").write_text("present\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)

    class Noop(Executor):
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            return WorkerTerminal(ok=True, output="RESULT: DONE already present", model=route_id)

    rt, ev, _ = make(repo, WorkGraph("g", (node("a"),)), Noop({}), ["cc/s"])
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert any(b["name"] == "no_change" for b in ev.of("barrier", "a"))


async def test_no_change_with_failing_check_is_rejected(repo: Path) -> None:
    class Noop(Executor):
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)

    rt, ev, _ = make(
        repo, WorkGraph("g", (node("a"),)), Noop({}), ["cc/s"], max_attempts_per_node=1
    )
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ev.of("verify", "a")[0]["ok"] is False
