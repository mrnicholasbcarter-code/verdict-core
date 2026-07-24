from __future__ import annotations

from datetime import datetime, timezone

import pytest

from verdict.availability import (
    AvailabilityState,
    CandidateRequirements,
    RuntimeObservation,
    explain_candidates,
    normalize_observation,
    select_capable_candidates,
)
from verdict.models import ModelInfo

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def test_stale_observation_becomes_unknown() -> None:
    observation = RuntimeObservation(
        observed_at=datetime(2026, 7, 16, 11, 58, tzinfo=timezone.utc),
        ttl_seconds=60,
        health="healthy",
        quota_remaining_pct=80,
        source="fixture:omniroute-api",
    )

    candidate = normalize_observation(
        ModelInfo(id="provider/model", provider="provider", capability_tier=2), observation, now=NOW
    )

    assert candidate.state is AvailabilityState.UNKNOWN
    assert "stale observation" in candidate.reasons


def test_runtime_states_distinguish_quota_cooldown_and_auth() -> None:
    base = dict(observed_at=NOW, ttl_seconds=60, source="fixture", health="healthy")
    assert (
        normalize_observation(
            ModelInfo(id="a", provider="p", capability_tier=2),
            RuntimeObservation(**base, quota_remaining_pct=0),
            now=NOW,
        ).state
        is AvailabilityState.QUOTA_EXHAUSTED
    )
    assert (
        normalize_observation(
            ModelInfo(id="b", provider="p", capability_tier=2),
            RuntimeObservation(**base, cooldown_until="2026-07-16T12:05:00Z"),
            now=NOW,
        ).state
        is AvailabilityState.RATE_LIMITED
    )
    assert (
        normalize_observation(
            ModelInfo(id="c", provider="p", capability_tier=2),
            RuntimeObservation(**base, auth="unauthorized"),
            now=NOW,
        ).state
        is AvailabilityState.UNAUTHORIZED
    )


def test_capability_filter_is_deterministic_and_explains_exclusions() -> None:
    candidates = [
        ModelInfo(
            id="p/vision",
            provider="p",
            capability_tier=1,
            capabilities=frozenset({"vision", "tools"}),
        ),
        ModelInfo(id="p/tools", provider="p", capability_tier=1, capabilities=frozenset({"tools"})),
    ]
    requirements = CandidateRequirements(required=frozenset({"vision", "tools"}))
    states = [
        normalize_observation(
            candidates[0],
            RuntimeObservation(observed_at=NOW, source="fixture", health="healthy"),
            now=NOW,
        ),
        normalize_observation(
            candidates[1],
            RuntimeObservation(observed_at=NOW, source="fixture", health="healthy"),
            now=NOW,
        ),
    ]

    eligible = select_capable_candidates(states, requirements)
    explanation = explain_candidates(states, requirements)

    assert [item.model.id for item in eligible] == ["p/vision"]
    assert explanation == [
        {
            "model": "p/tools",
            "state": "capability_mismatch",
            "rejected": True,
            "reason": "missing capability: vision",
        },
        {"model": "p/vision", "state": "eligible", "rejected": False, "reason": "eligible"},
    ]


def test_unknown_is_not_eligible_for_protected_work() -> None:
    state = normalize_observation(
        ModelInfo(id="p/model", provider="p", capability_tier=1),
        RuntimeObservation(observed_at=NOW, source="fixture", health="unknown"),
        now=NOW,
    )

    assert select_capable_candidates([state], CandidateRequirements(protected=True)) == []
    assert state.state is AvailabilityState.UNKNOWN


def test_stale_unknown_selection_and_explanation_use_the_same_gate() -> None:
    state = normalize_observation(
        ModelInfo(id="p/model", provider="p", capability_tier=1),
        RuntimeObservation(
            observed_at=datetime(2026, 7, 16, 11, 58, tzinfo=timezone.utc),
            ttl_seconds=60,
            source="fixture",
            health="healthy",
        ),
        now=NOW,
    )
    requirements = CandidateRequirements(unknown_is_eligible=True)

    assert select_capable_candidates([state], requirements) == []
    assert explain_candidates([state], requirements) == [
        {"model": "p/model", "state": "unknown", "rejected": True, "reason": "stale observation"}
    ]


def test_unknown_opt_in_never_overrides_missing_or_contradictory_evidence() -> None:
    model = ModelInfo(id="p/model", provider="p", capability_tier=1)
    missing_time = normalize_observation(
        model, RuntimeObservation(source="fixture", health="healthy", eligible=True), now=NOW
    )
    contradictory = normalize_observation(
        model,
        RuntimeObservation(observed_at=NOW, source="fixture", health="healthy", eligible=False),
        now=NOW,
    )

    assert (
        select_capable_candidates(
            [missing_time, contradictory], CandidateRequirements(unknown_is_eligible=True)
        )
        == []
    )


@pytest.mark.parametrize(
    "requirements",
    [
        {"budget_remaining": float("nan")},
        {"budget_remaining": -1},
        {"budget_remaining": 10**400},
        {"max_concurrency": "2"},
        {"max_concurrency": 0},
        {"protected": "false"},
        {"unknown_is_eligible": "false"},
        {"allow_degraded": "false"},
    ],
)
def test_invalid_candidate_capacity_requirements_are_rejected(requirements) -> None:
    with pytest.raises(ValueError):
        CandidateRequirements(**requirements)


def test_new_capacity_fields_preserve_legacy_positional_flags() -> None:
    requirements = CandidateRequirements(
        frozenset(), False, frozenset(), frozenset(), frozenset(), frozenset(), 1.0, 2, True, True
    )

    assert requirements.unknown_is_eligible is True
    assert requirements.allow_degraded is True
    assert requirements.estimated_tokens is None
    assert requirements.estimated_cost is None


def test_token_headroom_preserves_runtime_observation_positional_fields() -> None:
    observation = RuntimeObservation(NOW, 60, "fixture", "healthy", None, None, 1.0)

    assert observation.budget_remaining == 1.0
    assert observation.token_headroom is None


@pytest.mark.parametrize(
    ("required_alias", "canonical"),
    [("function_calling", "tools"), ("tool-calling", "tools"), ("json", "structured_output")],
)
def test_direct_requirements_canonicalize_legacy_capability_aliases(
    required_alias, canonical
) -> None:
    requirements = CandidateRequirements(required=frozenset({required_alias}))
    state = normalize_observation(
        ModelInfo(
            id="p/model", provider="p", capability_tier=1, capabilities=frozenset({canonical})
        ),
        RuntimeObservation(observed_at=NOW, health="healthy"),
        now=NOW,
    )

    assert requirements.required == frozenset({canonical})
    assert select_capable_candidates([state], requirements) == [state]


def test_allowed_degraded_explanation_preserves_runtime_state() -> None:
    state = normalize_observation(
        ModelInfo(id="p/model", provider="p", capability_tier=1),
        RuntimeObservation(observed_at=NOW, source="fixture", health="degraded"),
        now=NOW,
    )
    requirements = CandidateRequirements(allow_degraded=True)

    assert select_capable_candidates([state], requirements) == [state]
    assert explain_candidates([state], requirements) == [
        {"model": "p/model", "state": "degraded", "rejected": False, "reason": "health degraded"}
    ]
