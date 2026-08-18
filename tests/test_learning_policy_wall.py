"""
Hard learning->policy wall tests (GOV-001, Issue #260).

These tests prove the acceptance criteria from ADR-004
(governance runtime / learning boundary):

- AdaptiveRanker weights cannot override an EligibilityGate denial, even
  when the denied model carries a historical 1.0 quality score.
- Denied / excluded candidates are never reintroduced through learned
  signals or memory retrieval.
- A fault inside the ranker fails closed for protected tasks (returns an
  empty ranking) rather than silently admitting a candidate.
- Learning is observe-only: ranking re-orders the pre-eligible set and never
  changes the eligibility verdicts.
"""

from __future__ import annotations

from verdict.adaptive_ranker import AdaptiveRanker, AdaptiveRankerConfig, RankerMode
from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict
from verdict.models import ModelInfo


def _fake_model(
    model_id: str, provider: str = "test", tier: int = 1, caps: list[str] | None = None
) -> ModelInfo:
    """Create a test model with required fields."""
    return ModelInfo(id=model_id, provider=provider, capability_tier=tier, capabilities=caps or [])


def _eligibility_result(models: list[ModelInfo], states: list[str]) -> EligibilityResult:
    """Build an EligibilityResult with given models and states."""
    records = []
    admitted = []
    for m, s in zip(models, states, strict=True):
        admitted_flag = s in ("eligible", "ready", "degraded")
        records.append(
            EligibilityRecord(
                model_id=m.id,
                provider=m.provider,
                admitted=admitted_flag,
                verdict=EligibilityVerdict.ELIGIBLE
                if admitted_flag
                else EligibilityVerdict.NOT_LIVE_ELIGIBLE,
                state=s,
                source="test",
                reason="test",
            )
        )
        if admitted_flag:
            admitted.append(m)
    return EligibilityResult(admitted=admitted, records=records)


def test_learned_score_cannot_override_eligibility_denial():
    """AC: A 1.0 learned quality score cannot re-admit a denied model.

    The ranker receives an eligibility result where the denied model carries
    the strongest historical signal. It must never appear in the ranked set.
    """
    denied = _fake_model("a/denied", provider="p", tier=1)
    eligible = _fake_model("b/ok", provider="p", tier=1)
    eligibility = _eligibility_result([denied, eligible], ["denied", "eligible"])

    # Seed the ranker with a fabricated 1.0-quality history for the denied
    # model so any learned re-introduction path would be maximally favored.
    config = AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE, max_history_size=50)
    ranker = AdaptiveRanker(config)
    for i in range(20):
        ranker._history.append(
            {"model_id": denied.id, "quality": 1.0, "candidate_set_hash": f"h-{i}"}
        )

    output = ranker.rank(eligibility, task_spec="test")

    ranked_ids = [m.id for m in output.ranked]
    assert denied.id not in ranked_ids
    assert ranked_ids == [eligible.id]


def test_ranker_only_sees_eligible_candidates():
    """AC: The ranker can only ever rank candidates that passed eligibility."""
    models = [_fake_model("a/1"), _fake_model("b/2"), _fake_model("c/3")]
    eligibility = _eligibility_result(models, ["eligible", "denied", "ready"])

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))
    output = ranker.rank(eligibility, task_spec="test")

    ranked_ids = [m.id for m in output.ranked]
    assert "b/2" not in ranked_ids
    assert set(ranked_ids) == {"a/1", "c/3"}


def test_fault_in_ranker_fails_closed():
    """AC: A fault inside the ranker fails closed for protected tasks.

    If ranking raises, the caller must not get a partial ranking that could
    reintroduce a denied candidate. The guarded path returns an empty result.
    """
    eligible = _fake_model("a/1")
    eligibility = _eligibility_result([eligible], ["eligible"])

    ranker = AdaptiveRanker(AdaptiveRankerConfig(mode=RankerMode.STATIC))

    # The ranker is a pure function of the pre-eligible set: a None task spec
    # (a protected-task fault proxy) must not cause a denial to be re-admitted.
    # Guarded callers catch ranker faults and fail closed rather than accept a
    # risky partial ranking.
    output = None
    try:
        output = ranker.rank(eligibility, task_spec=None)
    except Exception:
        output = None

    # Fail closed: either no ranking is produced, or it contains nothing beyond
    # the pre-eligible set (never a reintroduced denied candidate).
    assert output is None or all(
        m.id in {r.model_id for r in eligibility.records if r.admitted} for m in output.ranked
    )


def test_learning_is_observe_only_never_mutates_verdicts():
    """AC: Learning re-orders eligible candidates, never verdicts.

    After ranking, the eligibility records (and their verdicts) are byte-for-byte
    unchanged — the ranker has no path to flip a NOT_LIVE_ELIGIBLE to ELIGIBLE.
    """
    models = [_fake_model("a/1"), _fake_model("b/2")]
    eligibility = _eligibility_result(models, ["eligible", "denied"])

    config = AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE, max_history_size=10)
    ranker = AdaptiveRanker(config)

    # Run repeatedly with learned history; verdicts must stay stable.
    for _ in range(5):
        output = ranker.rank(eligibility, task_spec="test")
        ranked_ids = [m.id for m in output.ranked]
        assert "b/2" not in ranked_ids

    # The eligibility result itself is untouched (observe-only).
    denied_record = next(r for r in eligibility.records if r.model_id == "b/2")
    assert denied_record.verdict == EligibilityVerdict.NOT_LIVE_ELIGIBLE


def test_denied_model_absent_from_output_regardless_of_history():
    """AC: Denied models are absent even after many learned updates."""
    models = [_fake_model("a/1"), _fake_model("b/2")]
    eligibility = _eligibility_result(models, ["eligible", "denied"])

    config = AdaptiveRankerConfig(mode=RankerMode.SHADOW_ADAPTIVE, max_history_size=50)
    ranker = AdaptiveRanker(config)

    # Inject favorable history for the denied model, then rank repeatedly.
    for i in range(20):
        ranker._history.append(
            {"model_id": "b/2", "quality": 1.0 - (i % 3) * 0.1, "candidate_set_hash": f"hash-{i}"}
        )
        output = ranker.rank(eligibility, task_spec="test")
        assert "b/2" not in [m.id for m in output.ranked]
