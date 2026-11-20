"""Capability escalation: after K judged failures on a weak route, the runtime
must move to a strictly stronger route.

Tests (failing-first per TDD):
1. Two judged failures on a weak route lead the third attempt to a stronger admitted route.
2. Escalation never selects a route excluded from the admitted set (exclude_routes respected).
3. With no stronger route the runtime falls back to the best available, budget bounded.
4. An interrupted (non-judged, e.g. quota) failure does not count toward escalation.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    NodeKind,
    NodeState,
    ReviewResult,
    RouteVerdict,
    RunOutcome,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Shared helpers (mirror test_orch_runtime.py)
# ---------------------------------------------------------------------------


class Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> None:
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["type"] == type and (node_id is None or r["node_id"] == node_id)
        ]


class EscalationSelector:
    """Tier-aware selector for escalation tests.

    Each route is registered as (route_id, capability_tier).  When
    requirements.escalation_max_tier is set, routes whose tier is strictly
    above the ceiling are excluded (returned with reason "below_escalation_floor"
    at TASK_ELIGIBLE stage, mirroring what the real eligibility ladder does).

    Routes in requirements.exclude_routes are always excluded regardless of tier.
    """

    def __init__(self, routes: list[tuple[str, int | None]]) -> None:
        self.routes = routes  # ordered by preference (first = cheapest / best)
        self.cool: dict[str, float] = {}
        self.successes: list[str] = []

    def evaluate(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict, ...]:
        return self.select(requirements, now=now)[1]

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        verdicts: list[RouteVerdict] = []
        chosen: RouteVerdict | None = None
        esc_max = requirements.escalation_max_tier

        for rank, (route, tier) in enumerate(self.routes, 1):
            provider = route.split("/")[0]

            if route in requirements.exclude_routes:
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.AVAILABLE,
                        EligibilityStage.TASK_ELIGIBLE,
                        "excluded",
                        capability_tier=tier,
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
                        capability_tier=tier,
                    )
                )
                continue

            # Apply escalation ceiling: routes too weak for the escalated floor are out.
            # When escalation is active, unknown-tier routes are also excluded
            # (they are weaker than any known tier).
            if esc_max is not None and (tier is None or tier > esc_max):
                reason = (
                    "unknown_capability_escalating" if tier is None else "below_escalation_floor"
                )
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.AVAILABLE,
                        EligibilityStage.TASK_ELIGIBLE,
                        reason,
                        capability_tier=tier,
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
                capability_tier=tier,
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
        return FailureClassification("unknown", "REROUTE", 120, "route", terminal.error)


class CallCountingExecutor:
    """Executor whose behaviour is a function of (node, route, call_number)."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        self.calls: list[tuple[str, str]] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        node = prompt.split(":")[0]
        call_num = sum(1 for n, r in self.calls if n == node and r == route_id) + 1
        self.calls.append((node, route_id))
        result = self._fn(node, route_id, call_num)

        if result == "ok":
            (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
            )
        elif result == "badcode":
            # Write FAIL so the verification_command check fails (badcode).
            (cwd / f"{node}.txt").write_text("FAIL\n")
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
            )
        elif result == "quota":
            return WorkerTerminal(ok=False, model=route_id, status_code=429, error="usage limit")
        else:
            raise ValueError(f"unknown behaviour {result!r}")


class Reviewer:
    async def review(self, **kwargs: Any) -> ReviewResult:
        return ReviewResult("PASS", "fake-ocr", "cx/reviewer", ())


def node(nid: str, kind: NodeKind = NodeKind.IMPLEMENT) -> WorkNode:
    target = f"{nid}.txt"
    check = ["sh", "-c", f"test -f {target} && ! grep -q FAIL {target}"]
    return WorkNode(
        nid, f"write {nid}", kind=kind, owned_files=(target,), verification_command=tuple(check)
    )


def make_esc(
    repo: Path,
    graph: WorkGraph,
    executor: CallCountingExecutor,
    routes_with_tiers: list[tuple[str, int | None]],
    reviewer: Reviewer | None = None,
    **policy: Any,
) -> tuple[DagRuntime, Events, EscalationSelector]:
    events = Events()
    selector = EscalationSelector(routes_with_tiers)
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run_esc",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_two_judged_failures_escalate_to_stronger_route(repo: Path) -> None:
    """After 2 judged failures on the weak route, the runtime picks a stronger route.

    Route pool: cc/haiku (tier 3, weak/cheap) listed first so it is always the
    first pick; cc/sonnet (tier 2, stronger) listed second.

    The first two attempts on cc/haiku write bad code so verification fails
    (validation path, validated=True).  On the first failure cc/haiku goes into
    rehydrated_routes and is retried; on the second it moves to tried and
    judged_failure_count reaches the threshold (2).  The third attempt must be
    dispatched on cc/sonnet via the escalation path.
    """

    # cc/haiku writes "FAIL" for all calls; cc/sonnet writes ok.
    def behaviour(node_id: str, route: str, call_num: int) -> str:
        if route == "cc/haiku":
            return "badcode"
        return "ok"  # cc/sonnet succeeds

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    rt, ev, _sel = make_esc(repo, graph, ex, [("cc/haiku", 3), ("cc/sonnet", 2)])

    result = await rt.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    # An escalation event must have been emitted.
    esc_events = ev.of("escalation")
    assert len(esc_events) >= 1, "expected at least one escalation event"
    esc = esc_events[0]
    assert esc["from_tier"] == 3, f"expected from_tier=3, got {esc['from_tier']}"
    assert esc["to_tier"] == 2, f"expected to_tier=2, got {esc['to_tier']}"
    assert esc["to_route"] == "cc/sonnet"
    assert esc["reason"] == "judged_failures_threshold"
    # The node must have been completed.
    assert result.nodes["n"].state is NodeState.VALIDATED


async def test_escalation_respects_exclude_routes_for_admitted_set(repo: Path) -> None:
    """Escalation must not select a route that is in exclude_routes (tried set).

    Route pool (ordered by selector preference):
      px/sonnet-tried (tier 2), cc/haiku (tier 3), cc/sonnet-new (tier 2)

    px/sonnet-tried is from a different provider (px) so a quota failure there
    does NOT cool the cc/* routes.  After px/sonnet-tried is expelled via quota
    (non-judged, goes into tried), cc/haiku gets 2 judged failures →
    escalation ceiling = 2.  The escalation path must pick cc/sonnet-new (tier 2,
    not in tried), never px/sonnet-tried (tier 2, in tried/excluded).
    """

    def behaviour(node_id: str, route: str, call_num: int) -> str:
        if route == "px/sonnet-tried":
            return "quota"  # non-judged, goes to tried
        if route == "cc/haiku":
            return "badcode"  # judged (verification_failed) x 2
        return "ok"  # cc/sonnet-new

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    rt, ev, _sel = make_esc(
        repo, graph, ex, [("px/sonnet-tried", 2), ("cc/haiku", 3), ("cc/sonnet-new", 2)]
    )

    result = await rt.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    # Escalation must have happened.
    esc_events = ev.of("escalation")
    assert len(esc_events) >= 1, "expected an escalation event"
    # The escalated-to route must be cc/sonnet-new, not px/sonnet-tried.
    assert esc_events[0]["to_route"] == "cc/sonnet-new", (
        f"escalation went to wrong route: {esc_events[0]['to_route']}"
    )
    # px/sonnet-tried must have been dispatched at most once (the quota attempt).
    terminal_events = ev.of("terminal", "n")
    dispatched = [e["route_id"] for e in terminal_events]
    sonnet_tried_count = dispatched.count("px/sonnet-tried")
    assert sonnet_tried_count <= 1, (
        f"px/sonnet-tried dispatched {sonnet_tried_count} times; "
        "escalation must not re-admit tried routes"
    )


async def test_escalation_fallback_when_no_stronger_route(repo: Path) -> None:
    """With no stronger route available, the runtime falls back to the best available
    without escalation, and the budget remains bounded.

    Route pool: cc/haiku-1 (tier 3) and cc/haiku-2 (tier 3) — both are weak.

    After 2 judged failures on cc/haiku-1 (attempt 1 rehydrated, attempt 2 moves
    to tried), the escalation ceiling is set to 2.  Escalation with ceiling=2
    finds no route with tier ≤ 2, so _esc_choice is None.  The fallback (no
    escalation_max_tier, exclude_routes={haiku-1}) selects cc/haiku-2.
    cc/haiku-2 succeeds → COMPLETE.  Critically, no escalation event must be
    emitted (the fallback path never fires "escalation").
    """

    def behaviour(node_id: str, route: str, call_num: int) -> str:
        if route == "cc/haiku-1":
            return "badcode"  # always bad; two failures → escalation ceiling set
        return "ok"  # cc/haiku-2

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    rt, ev, _sel = make_esc(repo, graph, ex, [("cc/haiku-1", 3), ("cc/haiku-2", 3)])

    result = await rt.run()

    # The run must COMPLETE: fallback picked cc/haiku-2 from the full range.
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    # No escalation event: escalation ceiling was set but the fallback path
    # was taken (no stronger route found, _esc_choice was None).
    esc_events = ev.of("escalation")
    assert len(esc_events) == 0, (
        f"unexpected escalation event when no stronger route exists: {esc_events}"
    )
    assert result.nodes["n"].state is NodeState.VALIDATED


async def test_non_judged_failure_does_not_count_toward_escalation(repo: Path) -> None:
    """Interrupted (non-judged) failures must not increment the escalation counter.

    Route pool: cc/haiku (tier 3) from provider cc, cx/sonnet (tier 2) from
    provider cx.

    cc/haiku returns quota (429).  The quota failure is REROUTE with
    scope="provider", which cools the entire cc provider.  cx/sonnet (different
    provider) is unaffected.  The runtime reroutes to cx/sonnet, which succeeds.
    Critically: judged_failure_count must stay 0 and no escalation event emitted.
    """

    def behaviour(node_id: str, route: str, call_num: int) -> str:
        if route == "cc/haiku":
            return "quota"  # non-judged, REROUTE
        return "ok"  # cx/sonnet from a different provider

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    # cc/haiku and cx/sonnet are on different providers so quota-cooling cc
    # does not affect cx/sonnet.
    rt, ev, _sel = make_esc(repo, graph, ex, [("cc/haiku", 3), ("cx/sonnet", 2)])

    result = await rt.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    # No escalation event: quota failures are not judged failures.
    esc_events = ev.of("escalation")
    assert len(esc_events) == 0, (
        f"unexpected escalation event from non-judged (quota) failure: {esc_events}"
    )
    assert result.nodes["n"].state is NodeState.VALIDATED


async def test_unknown_tier_judged_failures_escalate_to_known_tier_route(repo: Path) -> None:
    """Two judged failures on a route with unknown capability_tier (None) must escalate
    to a route with a known tier on the third attempt.

    Unknown-tier routes represent free or uncatalogued models where the name-based
    classifier has no entry.  After K=2 judged failures the runtime sets
    escalation_tier_ceiling=_ESCALATION_UNKNOWN_TIER_CEILING (3).  The eligibility
    check then excludes all unknown-tier routes (reason 'unknown_capability_escalating')
    so the escalation path selects only routes with a measured capability_tier.

    The 'escalation' event must be emitted with from_tier=None.
    """

    # cc/free has tier=None (unknown); cx/known has tier=2 (known).
    def behaviour(node_id: str, route: str, call_num: int) -> str:
        if route == "cc/free":
            return "badcode"  # judged failure x 2 (rehydrate then tried)
        return "ok"  # cx/known succeeds

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    rt, ev, _sel = make_esc(repo, graph, ex, [("cc/free", None), ("cx/known", 2)])

    result = await rt.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    # An escalation event must be emitted with from_tier=None.
    esc_events = ev.of("escalation")
    assert len(esc_events) >= 1, "expected an escalation event from unknown-tier failures"
    esc = esc_events[0]
    assert esc["from_tier"] is None, f"expected from_tier=None, got {esc['from_tier']}"
    assert esc["to_route"] == "cx/known"
    assert esc["to_tier"] == 2
    assert esc["reason"] == "judged_failures_threshold"
    assert result.nodes["n"].state is NodeState.VALIDATED


async def test_tier1_route_escalation_exhausted_emits_event_and_budget_bounded(repo: Path) -> None:
    """When the last-selected route is at tier=1 (the strongest known tier), neither a
    stronger route ceiling nor an unknown-tier ceiling can be set.

    The runtime must emit an 'escalation_exhausted' event (reason='at_strongest_tier')
    and must NOT hang: once the pool is exhausted the node ends BLOCKED.

    Failing-first: before the else-branch was added, reaching t==1 was a silent no-op
    — no event was emitted and no test caught it.
    """

    def behaviour(node_id: str, route: str, call_num: int) -> str:
        return "badcode"  # always fails judged (verification_failed)

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    # Only one route at tier=1 (frontier): no stronger route exists.
    rt, ev, _sel = make_esc(repo, graph, ex, [("cc/tier1-only", 1)])

    result = await rt.run()

    # Budget is bounded: the node must not hang.
    assert result.outcome is not RunOutcome.COMPLETE
    # 'escalation_exhausted' must be emitted with the correct reason and from_tier.
    exhaust = ev.of("escalation_exhausted")
    assert len(exhaust) >= 1, "expected an escalation_exhausted event when at strongest tier"
    assert exhaust[0]["reason"] == "at_strongest_tier"
    assert exhaust[0]["from_tier"] == 1


async def test_unknown_tier_escalation_succeeds_when_ceiling_is_below_3(repo: Path) -> None:
    """Escalation ceiling=2 (< 3) must still select a known tier=1 route after two
    judged failures on an unknown-tier route.

    Before the reorder in eligibility._assess the '_effective_max_tier < 3' check fired
    first (min(3,2)=2 < 3), returning 'unknown_capability' and treating the route as if
    no escalation was active.  After the fix the escalation check fires first, returning
    'unknown_capability_escalating' for every active ceiling value, so the runtime
    can still select the tier=1 route whose tier ≤ ceiling.

    Failing-first: add this test, run it against the original ordering, confirm it
    fails (escalation path broken), then apply the reorder and confirm it passes.
    """

    def behaviour(node_id: str, route: str, call_num: int) -> str:
        if route == "cc/free":
            return "badcode"
        return "ok"

    ex = CallCountingExecutor(behaviour)
    graph = WorkGraph("g", (node("n"),))
    # cc/free tier=None (unknown); cc/strong tier=1 (≤ ceiling=2 → admitted)
    rt, ev, _sel = make_esc(repo, graph, ex, [("cc/free", None), ("cc/strong", 1)])

    result = await rt.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    esc_events = ev.of("escalation")
    assert len(esc_events) >= 1, "expected an escalation event"
    esc = esc_events[0]
    assert esc["from_tier"] is None
    assert esc["to_route"] == "cc/strong"
