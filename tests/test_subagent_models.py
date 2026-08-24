"""Eligibility-before-ranking guarantees for role-based subagent selection.

Spec 272 AC-1.5: no candidate excluded by deterministic eligibility may be
restored by gateway scoring, historical reputation, learned ranking, or
fallback. These tests pin that contract at the `select_for_role` seam, which
previously re-derived its own admission set from raw candidate state and so
readmitted candidates the eligibility pass had already excluded.
"""

from __future__ import annotations

import pytest

from verdict.availability import (
    AvailabilityCandidate,
    AvailabilityReport,
    AvailabilityState,
    CandidateRequirements,
    is_opaque_route_id,
)
from verdict.availability_cache import AvailabilityCache
from verdict.eligibility import EligibilityGate
from verdict.models import ModelInfo
from verdict.subagent_models import SubagentModelSelector


def _model(model_id: str, *, tier: int = 2, capabilities: tuple[str, ...] = ("tools", "reasoning")):
    return ModelInfo(
        id=model_id,
        provider=model_id.split("/", 1)[0],
        capability_tier=tier,
        context_window=200_000,
        capabilities=frozenset(capabilities),
        is_available=True,
        availability_state=AvailabilityState.READY.value,
        source="catalog",
    )


def _candidate(model_id: str, state: AvailabilityState = AvailabilityState.READY, **kw):
    return AvailabilityCandidate(
        model=_model(model_id, **kw),
        state=state,
        reasons=(),
        headroom_pct=50.0,
        source="test",
        freshness_seconds=1.0,
    )


class _StubIntelligence:
    """Ranks by reverse id so ordering never coincides with input order."""

    def __init__(self) -> None:
        self.ranked_inputs: list[list[str]] = []

    async def rank(self, models, task_spec):
        self.ranked_inputs.append([m.id for m in models])
        ordered = sorted(models, key=lambda m: m.id, reverse=True)

        ranked = [type("R", (), {"model_id": m.id, "score": 1.0})() for m in ordered]
        return type("_Ranking", (), {"ranked": ranked})()


def _selector(candidates, eligible):
    report = AvailabilityReport(
        candidates=tuple(candidates),
        eligible=tuple(eligible),
        source="test",
        freshness_seconds=1.0,
    )

    def source(requirements: CandidateRequirements) -> AvailabilityReport:
        return report

    cache = AvailabilityCache(source=source, ttl_seconds=60, stale_window_seconds=30)
    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    intelligence = _StubIntelligence()
    selector = SubagentModelSelector(
        availability_cache=cache, eligibility_gate=gate, intelligence=intelligence
    )
    return selector, intelligence


def test_candidate_excluded_by_eligibility_is_never_selected():
    """AC-1.5: a high-ranking candidate absent from `eligible` must not win."""
    winner = _candidate("zz/excluded-but-top-ranked")
    keeper = _candidate("aa/eligible")
    selector, intelligence = _selector([winner, keeper], eligible=[keeper])

    chosen = selector.select_for_role("worker")

    assert chosen is not None
    assert chosen.id == "aa/eligible"
    # The excluded candidate must never even reach the ranker.
    assert intelligence.ranked_inputs == [["aa/eligible"]]


def test_empty_eligible_set_returns_none_rather_than_a_candidate():
    """Zero eligible must fail closed, not fall back to the candidate pool."""
    pool = [_candidate("aa/one"), _candidate("bb/two")]
    selector, intelligence = _selector(pool, eligible=[])

    assert selector.select_for_role("worker") is None
    assert intelligence.ranked_inputs == []


def test_degraded_candidate_admitted_only_when_report_says_eligible():
    """`allow_degraded` is the adapter's decision; selection must not re-derive it."""
    degraded = _candidate("aa/degraded", state=AvailabilityState.DEGRADED)
    selector, _ = _selector([degraded], eligible=[])
    assert selector.select_for_role("worker") is None

    selector_ok, _ = _selector([degraded], eligible=[degraded])
    chosen = selector_ok.select_for_role("scout")
    assert chosen is not None and chosen.id == "aa/degraded"


@pytest.mark.parametrize(
    "model_id",
    ["auto", "auto/coding", "combo/anything", "router/x", "virtual/y", "default"],
)
def test_opaque_resolver_aliases_are_excluded_even_when_eligible(model_id):
    """Opaque routing is denied for protected work regardless of gateway verdict."""
    assert is_opaque_route_id(model_id)
    opaque = _candidate(model_id)
    concrete = _candidate("aa/concrete")
    selector, _ = _selector([opaque, concrete], eligible=[opaque, concrete])

    chosen = selector.select_for_role("worker")
    assert chosen is not None and chosen.id == "aa/concrete"


def test_opaque_rule_does_not_read_a_gateway_specific_provider_field():
    """D-005 gateway neutrality: a concrete id is kept whatever its provider says.

    OmniRoute marks combo pseudo-routes with `owned_by: combo`. Branching on
    that field is gateway detection; only the id shape may decide.
    """
    combo_provider = AvailabilityCandidate(
        model=ModelInfo(
            id="anthropic/claude-real",
            provider="combo",
            capability_tier=2,
            context_window=200_000,
            capabilities=frozenset({"tools", "reasoning"}),
            is_available=True,
            availability_state=AvailabilityState.READY.value,
            source="catalog",
        ),
        state=AvailabilityState.READY,
        reasons=(),
        headroom_pct=50.0,
        source="test",
        freshness_seconds=1.0,
    )
    selector, _ = _selector([combo_provider], eligible=[combo_provider])

    chosen = selector.select_for_role("worker")
    assert chosen is not None and chosen.id == "anthropic/claude-real"


def test_diversity_exclusion_actually_excludes_the_family():
    """Previously injected `family/*` wildcards into deny_models and never fired."""
    same_family = _candidate("zz/sibling")
    other_family = _candidate("aa/other")
    selector, _ = _selector([same_family, other_family], eligible=[same_family, other_family])

    # Without diversity the reverse-id ranker prefers zz/sibling.
    assert selector.select_for_role("worker").id == "zz/sibling"

    chosen = selector.select_for_role("worker", diversity_from=["zz/already-used"])
    assert chosen is not None and chosen.id == "aa/other"


def test_diversity_exclusion_can_empty_the_set_and_fails_closed():
    only = _candidate("zz/only")
    selector, _ = _selector([only], eligible=[only])
    assert selector.select_for_role("worker", diversity_from=["zz/other"]) is None


def test_unknown_role_is_rejected():
    selector, _ = _selector([_candidate("aa/x")], eligible=[_candidate("aa/x")])
    with pytest.raises(ValueError, match="Unknown role"):
        selector.select_for_role("not-a-role")
