"""Admitted routes are ranked by a verified success score within a capacity class (#1216, slice 1).

The score reorders routes that are already admitted. It never admits, never moves a route to
another capacity class, and a route without enough verified results is neutral.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import NOW, FakeProbe, conn, row
from verdict.orchestration.candidate_builder import build_candidates
from verdict.orchestration.contracts import NodeKind, TaskRequirements, WorkNode
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.orchestration.session_evidence import SessionLedger, SessionOutcome

COLD = "zz/a-model"  # sorts first on the final route_id tie-break
PROVEN = "zz/b-model"
FREE = "gl/glm-5"
ZERO = {"input": 0.0, "output": 0.0}


@pytest.fixture(autouse=True)
def _subscription(billing_overlay_subscription: Callable[[list[tuple[str, str]]], None]) -> None:
    billing_overlay_subscription([("zz", "oauth")])


def outcome(
    route: str,
    passed: bool,
    *,
    node_kind: str | None = "implement",
    hours: int = 1,
    kind: str = "real_task",
    verified: bool = True,
) -> SessionOutcome:
    return SessionOutcome(
        route,
        "pass" if passed else "fail",
        kind,
        None if passed else "wrong_result",
        verified,
        NOW - timedelta(hours=hours),
        "verdict.orchestration.receipt",
        node_kind=node_kind,
    )


def record(
    ledger: SessionLedger, route: str, passes: int, fails: int, *, start: int = 1, **kwargs: Any
) -> None:
    """Distinct timestamps: the ledger de-duplicates on route, time and outcome.

    A second call for the same route needs its own ``start`` hour.
    """
    for i in range(passes):
        ledger.append(outcome(route, True, hours=start + i, **kwargs))
    for i in range(fails):
        ledger.append(outcome(route, False, hours=start + passes + i, **kwargs))


def build(tmp_path: Path, ledger: SessionLedger | None, **kwargs: Any) -> EligibilityLadder:
    rows = [
        row(COLD, owned_by="zz", pricing={"input": 1.0, "output": 2.0}),
        row(PROVEN, owned_by="zz", pricing={"input": 1.0, "output": 2.0}),
        row(FREE, owned_by="glm", pricing=ZERO),
    ]
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record(
        FREE,
        ProbeResult(
            category=CATEGORY_OK, chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        ),
        NOW,
    )
    return EligibilityLadder(
        rows,
        [conn("zz"), conn("glm", auth="apikey", plan="free", free_only=True)],
        FakeProbe(),
        tmp_path / "state.json",
        health_cache=cache,
        session_ledger=ledger,
        **kwargs,
    )


def worker_requirements(kind: NodeKind = NodeKind.IMPLEMENT) -> TaskRequirements:
    """What the runtime builds for a node: the same path production takes."""
    extra: dict[str, Any] = (
        {"owned_files": ("src/x.py",), "verification_command": ("true",)}
        if kind is NodeKind.IMPLEMENT
        else {}
    )
    return TaskRequirements.for_node(WorkNode(node_id="n1", objective="do it", kind=kind, **extra))


def order(ladder: EligibilityLadder, requirements: TaskRequirements) -> list[str]:
    _, verdicts = ladder.select(requirements, now=NOW)
    ranked = [v for v in verdicts if v.rank is not None]
    return [v.route_id for v in sorted(ranked, key=lambda v: v.rank or 0)]


def success(
    ladder: EligibilityLadder, requirements: TaskRequirements, route: str
) -> dict[str, Any]:
    _, verdicts = ladder.select(requirements, now=NOW)
    components = {v.route_id: v.rank_components for v in verdicts}[route]
    assert components is not None
    value = components["success"]
    assert isinstance(value, dict)
    return value


def test_for_node_carries_the_node_kind() -> None:
    assert worker_requirements(NodeKind.RESEARCH).node_kind == "research"
    assert TaskRequirements().node_kind is None


def test_recent_failures_for_this_kind_rank_below_a_cold_route(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, COLD, 0, 4)  # four verified implement failures
    record(ledger, COLD, 2, 2, node_kind="research", start=10)  # a neutral record for research
    ladder = build(tmp_path, ledger)
    # The cold route and the failing route share a class; the failing route used to win
    # the route_id tie-break and now ranks below the route with no results.
    assert order(ladder, worker_requirements(NodeKind.IMPLEMENT)) == [FREE, PROVEN, COLD]
    # For another node kind the same route ranks as it did before the score existed.
    assert order(ladder, worker_requirements(NodeKind.RESEARCH)) == [FREE, COLD, PROVEN]


def test_many_verified_passes_rank_above_a_cold_route_never_above_a_cheaper_class(
    tmp_path: Path,
) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 6, 0)
    ladder = build(tmp_path, ledger)
    # PROVEN beats the cold subscription route; the FREE route, which has no results,
    # still comes first because the capacity class is the first key.
    assert order(ladder, worker_requirements()) == [FREE, PROVEN, COLD]
    # Even a FREE route with failures (score below the subscription route) stays first
    # when it is still admitted: keep the pooled record above the gate's 0.5 line.
    record(ledger, FREE, 4, 0, node_kind="research", start=10)
    record(ledger, FREE, 0, 3)
    assert order(ladder, worker_requirements()) == [FREE, PROVEN, COLD]


def test_below_the_minimum_sample_the_key_falls_back_to_the_route(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, COLD, 0, 2)  # two implement failures: under the minimum of three
    record(ledger, COLD, 0, 2, node_kind="research", start=10)
    ladder = build(tmp_path, ledger)
    requirements = worker_requirements()
    fallback = success(ladder, requirements, COLD)
    assert fallback["key"] == "route"
    assert fallback["n"] == 4
    assert fallback["source"] == "session_ledger"
    assert fallback["score"] < 0.5
    assert order(ladder, requirements) == [FREE, PROVEN, COLD]
    # With enough results for the kind itself the key is route + kind.
    record(ledger, COLD, 0, 1, start=20)
    own = success(ladder, requirements, COLD)
    assert (own["key"], own["n"]) == ("route+kind", 3)


def test_a_route_below_the_minimum_everywhere_is_neutral(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 2, 0)  # two passes: not enough for any key
    ladder = build(tmp_path, ledger)
    requirements = worker_requirements()
    neutral = success(ladder, requirements, PROVEN)
    assert (neutral["score"], neutral["n"], neutral["key"]) == (0.5, 2, None)
    assert neutral["source"] == "below_min_sample"
    assert order(ladder, requirements) == [FREE, COLD, PROVEN]  # unchanged tie-break


def test_the_receipt_records_the_settings_and_the_source(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 5, 0)
    ladder = build(tmp_path, ledger, success_window_days=10, success_min_sample=4)
    selected, verdicts = ladder.select(worker_requirements(), now=NOW)
    assert selected is not None
    candidates, _, _ = build_candidates(verdicts, selected.route_id)
    by_route = {c["route_id"]: c["rank_components"]["success"] for c in candidates}
    assert by_route[PROVEN] == {
        "score": pytest.approx(by_route[PROVEN]["score"]),
        "n": 5,
        "key": "route+kind",
        "node_kind": "implement",
        "source": "session_ledger",
        "window_days": 10,
        "min_sample": 4,
    }
    assert by_route[PROVEN]["score"] > 0.8


def test_the_score_never_changes_membership_or_a_rejection_reason(tmp_path: Path) -> None:
    requirements = worker_requirements()
    bare = build(tmp_path / "bare", None)
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 8, 0)
    record(ledger, COLD, 0, 5)
    # The FREE route has many verified passes but no agentic proof: still refused.
    record(ledger, FREE, 9, 0)
    scored = build(tmp_path / "scored", ledger)
    (tmp_path / "bare").mkdir(exist_ok=True)
    cache = scored._health_cache
    assert cache is not None
    cache._routes.pop(FREE)
    bare_cache = bare._health_cache
    assert bare_cache is not None
    bare_cache._routes.pop(FREE, None)

    def membership(ladder: EligibilityLadder) -> dict[str, tuple[Any, Any, str]]:
        return {
            v.route_id: (v.reached, v.failed_stage, v.reason)
            for v in ladder.evaluate(requirements, now=NOW)
        }

    before, after = membership(bare), membership(scored)
    assert before == after
    assert after[FREE][2] == "no_agentic_probe"
    # The pairing assertion: the score is really in use, so the equality above is not vacuous.
    assert success(scored, requirements, PROVEN)["key"] == "route+kind"


@pytest.mark.parametrize(
    "tweak",
    [
        {"kind": "free_real_task"},  # imported from the controller's older ledger
        {"kind": "session_canary_v1"},  # a probe
        {"verified": False},
    ],
)
def test_only_verified_real_work_counts(tmp_path: Path, tweak: dict[str, Any]) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 6, 0, **tweak)
    ladder = build(tmp_path, ledger)
    requirements = worker_requirements()
    result = success(ladder, requirements, PROVEN)
    assert (result["score"], result["n"], result["key"]) == (0.5, 0, None)
    assert order(ladder, requirements) == [FREE, COLD, PROVEN]


def test_results_outside_the_window_do_not_count(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    for i in range(4):
        ledger.append(outcome(PROVEN, True, hours=24 * 15 + i))
    ladder = build(tmp_path, ledger)
    assert success(ladder, worker_requirements(), PROVEN)["n"] == 0


def test_aliases_of_one_model_share_a_record(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    # agy/ and antigravity/ are one backend pool in the provider catalog.
    for i, route in enumerate(("agy/claude-x", "antigravity/claude-x", "agy/claude-x")):
        ledger.append(outcome(route, True, hours=1 + i))
    rows = [row("antigravity/claude-x", owned_by="antigravity", pricing=ZERO)]
    cache = HealthCache(tmp_path / "h.json")
    cache.record(
        "antigravity/claude-x",
        ProbeResult(
            category=CATEGORY_OK, chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        ),
        NOW,
    )
    ladder = EligibilityLadder(
        rows,
        [conn("antigravity", auth="apikey", plan="free", free_only=True)],
        FakeProbe(),
        tmp_path / "state.json",
        health_cache=cache,
        session_ledger=ledger,
    )
    verdicts = ladder.evaluate(worker_requirements(), now=NOW)
    components = verdicts[0].rank_components
    assert components is not None
    assert components["success"]["n"] == 3


@pytest.mark.parametrize(("requirements", "reason"), [(TaskRequirements(), "no_node_kind")])
def test_without_a_node_kind_every_route_is_neutral(
    tmp_path: Path, requirements: TaskRequirements, reason: str
) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 6, 0)
    ladder = build(tmp_path, ledger)
    result = success(ladder, requirements, PROVEN)
    assert (result["score"], result["n"], result["key"], result["source"]) == (
        None,
        None,
        None,
        reason,
    )
    assert order(ladder, requirements) == [FREE, COLD, PROVEN]


def test_no_ledger_is_neutral_with_a_reason(tmp_path: Path) -> None:
    ladder = build(tmp_path, None)
    result = success(ladder, worker_requirements(), PROVEN)
    assert (result["score"], result["source"]) == (None, "no_ledger")


def test_an_unreadable_ledger_fails_open_with_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 6, 0)
    reads = 0
    real_load = ledger.load

    def counting() -> tuple[SessionOutcome, ...]:
        nonlocal reads
        reads += 1
        return real_load()

    monkeypatch.setattr(ledger, "load", counting)
    ledger.path.chmod(0o000)
    try:
        ladder = build(tmp_path, ledger)
        requirements = worker_requirements()
        assert order(ladder, requirements) == [FREE, COLD, PROVEN]
        assert success(ladder, requirements, PROVEN)["source"] == "unreadable"
    finally:
        ledger.path.chmod(0o600)
    assert reads == 2  # one per selection, never one per route


def test_one_read_per_selection_not_one_per_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    record(ledger, PROVEN, 6, 0)
    ladder = build(tmp_path, ledger)
    reads = 0
    real_load = ledger.load

    def counting() -> tuple[SessionOutcome, ...]:
        nonlocal reads
        reads += 1
        return real_load()

    monkeypatch.setattr(ledger, "load", counting)
    ladder.select(TaskRequirements(), now=NOW)  # no node kind: no success rank
    without = reads
    reads = 0
    ladder.select(worker_requirements(), now=NOW)
    # The existing pooled summary reads once per route; the success rank adds exactly one.
    assert reads - without == 1


@pytest.mark.parametrize(
    "bad", [{"success_window_days": 0}, {"success_window_days": 31}, {"success_min_sample": 0}]
)
def test_settings_are_validated(tmp_path: Path, bad: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        build(tmp_path, None, **bad)


def test_a_new_selection_reads_the_ledger_again(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    ladder = build(tmp_path, ledger)
    requirements = worker_requirements()
    assert success(ladder, requirements, COLD)["n"] == 0
    record(ledger, COLD, 0, 4)
    assert success(ladder, requirements, COLD)["n"] == 4
    later = replace(requirements, node_kind="research")
    assert success(ladder, later, COLD)["key"] == "route"
