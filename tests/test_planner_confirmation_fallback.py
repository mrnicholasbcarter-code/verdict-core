"""Offline regressions for bounded planner confirmation fallback."""

from pathlib import Path

import pytest

from tests.test_orch_eligibility import NOW, FakeProbe, conn, row
from verdict.admission import RuntimeEvidence, admit
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.subagent_selection import HealthResult

FREE = "fmd/gpt-6-astra"
SUB = "codex/gpt-5.5"
REQ = TaskRequirements(frontier_worthy=True, max_capability_tier=2)


def ladder(tmp_path: Path, *, fresh_free: bool = False, sub_ok: bool = True):
    cold = [f"ghost/deepseek-v3-{i}" for i in range(10)]
    rows = [row(r) for r in cold] + [row(FREE, "freemodel-dev"), row(SUB)]
    connections = [conn("ghost", plan="free"), conn("freemodel-dev", plan="free"), conn("codex")]
    cache = HealthCache(tmp_path / "health-cache.json")
    for route in [SUB] + ([FREE] if fresh_free else []):
        cache.record(route, ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    probe = FakeProbe({r: HealthResult(False, "unservable") for r in [*cold, FREE]})
    probe.results[SUB] = HealthResult(sub_ok, "ok" if sub_ok else "unservable")
    if fresh_free:
        probe.results[FREE] = HealthResult(True, "ok")
    admitted = admit(rows, connections, RuntimeEvidence(), now=NOW)
    selector = EligibilityLadder(
        rows,
        connections,
        probe,
        tmp_path / "state.json",
        health_cache=cache,
        admitted=admitted,
        max_probes_per_select=8,
    )
    return selector, probe, cold


def test_fresh_free_confirms_before_cold_aliases(tmp_path: Path) -> None:
    selector, probe, _ = ladder(tmp_path, fresh_free=True)
    ranked = selector.evaluate(REQ, now=NOW)
    assert next(v for v in ranked if v.route_id == FREE).rank != 0
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is not None and choice.route_id == FREE
    assert probe.calls == [FREE]
    assert selector.admitted.launchable(FREE)


def test_last_slot_confirms_fresh_subscription_fallback(tmp_path: Path) -> None:
    selector, probe, _ = ladder(tmp_path)
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is not None and choice.route_id == SUB
    assert len(probe.calls) == 8 and probe.calls[-1] == SUB
    assert choice.reason == "reserved_fallback_slot"
    assert selector.admitted.launchable(SUB)


def test_failed_fallback_never_launches_or_readmits(tmp_path: Path) -> None:
    selector, probe, _ = ladder(tmp_path, sub_ok=False)
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is None
    assert len(probe.calls) == 8 and probe.calls[-1] == SUB
    assert SUB not in selector.admitted


def test_confirmed_cold_free_still_wins(tmp_path: Path) -> None:
    selector, probe, cold = ladder(tmp_path)
    probe.results[cold[0]] = HealthResult(True, "ok")
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is not None and choice.route_id == cold[0]
    assert probe.calls == [cold[0]]


@pytest.mark.parametrize("block", ["excluded", "refresh", "cooldown"])
def test_blocked_fallback_is_not_confirmed(tmp_path: Path, block: str) -> None:
    from dataclasses import replace

    from verdict.orchestration.contracts import FailureClassification

    selector, probe, _ = ladder(tmp_path)
    req = REQ
    if block == "excluded":
        req = replace(req, exclude_routes=frozenset({SUB}))
    elif block == "refresh":
        selector._refresh_hook = lambda ids, now: {SUB: "failed"}
    else:
        selector.record_failure(
            SUB, FailureClassification("no_final_answer", "REROUTE", 300, "route"), now=NOW
        )
    choice, _ = selector.select(req, now=NOW)
    assert choice is None and SUB not in probe.calls
    assert len(probe.calls) == 8


@pytest.mark.asyncio
async def test_planner_event_records_reserved_confirmation(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from verdict.orchestration.contracts import WorkerTerminal
    from verdict.orchestration.receipt import EventLog
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    selector, _, _ = ladder(tmp_path)
    monkeypatch.setattr("verdict.orchestration.planner.repo_map", lambda repo: "offline map")

    async def run(*args, **kwargs):
        return WorkerTerminal(
            ok=True, output='{"nodes":[{"node_id":"r","objective":"research","kind":"research"}]}'
        )

    events = EventLog(tmp_path / "run" / "events.jsonl")
    await plan_with_failover(
        "goal",
        repo=tmp_path,
        selector=selector,
        executor=SimpleNamespace(run=run),
        classifier=FailureIntelligence(),
        events=events,
        now=lambda: NOW,
    )
    event = next(e for e in events.read() if e.type == "plan_started")
    assert event.data["selection_reason"] == "reserved_fallback_slot"


def test_reserved_slot_counts_probes_spent_not_list_position(tmp_path: Path) -> None:
    """Review: skipped (refresh-failed) entries must not move the fallback ahead of cold probes.

    Two cold routes are already marked failed by the refresh hook, so they use no
    probe. The fallback must still wait until 7 real probes were spent.
    """
    selector, probe, cold = ladder(tmp_path)
    selector._refresh_hook = lambda routes, now: {cold[0]: "failed", cold[1]: "failed"}
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is not None and choice.route_id == SUB
    assert len(probe.calls) == 8 and probe.calls[-1] == SUB
    assert cold[0] not in probe.calls and cold[1] not in probe.calls


def test_refresh_written_positive_is_used_in_the_same_select(tmp_path: Path) -> None:
    """Cold home (CI): health is empty at assessment; the refresh job writes a
    fresh positive for the subscription route; that same select must use it as
    the reserved fallback instead of ending with no planner."""
    selector, probe, _cold = ladder(tmp_path)
    cache = selector._health_cache
    # Start cold: drop the pre-seeded subscription positive.
    cache._routes.pop(SUB, None)

    def hook(routes, now):
        cache.record(SUB, ProbeResult(category="ok", chat_ok=True, tool_ok=True), now)
        cache.save()  # the real refresh job persists before returning
        return {SUB: "ok"}

    selector._refresh_hook = hook
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is not None and choice.route_id == SUB
    assert choice.reason == "reserved_fallback_slot"
    assert len(probe.calls) == 8 and probe.calls[-1] == SUB


def test_refresh_written_by_another_cache_object_is_seen(tmp_path: Path) -> None:
    """The real refresh job writes through its own HealthCache object to disk."""
    selector, _probe, _cold = ladder(tmp_path)
    cache = selector._health_cache
    cache._routes.pop(SUB, None)
    cache.save()

    def hook(routes, now):
        writer = HealthCache(cache.path)
        writer.record(SUB, ProbeResult(category="ok", chat_ok=True, tool_ok=True), now)
        writer.save()
        return {SUB: "ok"}

    selector._refresh_hook = hook
    choice, _ = selector.select(REQ, now=NOW)
    assert choice is not None and choice.route_id == SUB
    assert choice.reason == "reserved_fallback_slot"
