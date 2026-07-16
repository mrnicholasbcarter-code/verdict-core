from datetime import datetime, timezone

import pytest

from llm_gate.availability import (
    AvailabilityState,
    CandidateRequirements,
    OmniRouteAvailabilityAdapter,
    RuntimeObservation,
    StaticOmniRouteTransport,
    normalize_catalog,
    normalize_observation,
)
from llm_gate.models import ModelInfo

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
MODEL = ModelInfo(id="p/model", provider="p", capability_tier=1, capabilities=frozenset({"tools"}))


@pytest.mark.parametrize(
    ("observation", "state"),
    [
        (
            RuntimeObservation(
                observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=0
            ),
            AvailabilityState.QUOTA_EXHAUSTED,
        ),
        (
            RuntimeObservation(
                observed_at=NOW,
                source="fixture",
                health="healthy",
                cooldown_until="2026-07-16T12:05:00Z",
            ),
            AvailabilityState.RATE_LIMITED,
        ),
        (
            RuntimeObservation(
                observed_at=NOW,
                source="fixture",
                health="healthy",
                lockout_until="2026-07-16T12:05:00Z",
            ),
            AvailabilityState.LOCKED_OUT,
        ),
        (
            RuntimeObservation(observed_at=NOW, source="fixture", health="healthy", circuit="open"),
            AvailabilityState.CIRCUIT_OPEN,
        ),
        (
            RuntimeObservation(
                observed_at=NOW, source="fixture", health="healthy", auth="unauthorized"
            ),
            AvailabilityState.UNAUTHORIZED,
        ),
    ],
)
def test_hard_runtime_exclusions_are_normalized(observation, state):
    assert normalize_observation(MODEL, observation, now=NOW).state is state


def test_contradictory_and_malformed_observations_are_unknown_or_malformed():
    contradictory = normalize_observation(
        MODEL, RuntimeObservation(observed_at=NOW, health="healthy", eligible=False), now=NOW
    )
    malformed = normalize_observation(
        MODEL, RuntimeObservation(observed_at="not-a-time", health="healthy"), now=NOW
    )
    assert contradictory.state is AvailabilityState.UNKNOWN
    assert "contradictory" in contradictory.reasons[0]
    assert malformed.state is AvailabilityState.MALFORMED


def test_timeout_and_catalog_malformed_data_are_failure_isolated():
    class TimeoutTransport:
        def catalog(self):
            return {"data": [{"id": "p/model", "provider": "p"}]}

        def runtime(self):
            raise TimeoutError

    report = OmniRouteAvailabilityAdapter(TimeoutTransport()).evaluate(now=NOW)
    assert report.candidates[0].state is AvailabilityState.TIMEOUT
    assert report.eligible == ()
    assert normalize_catalog({"unexpected": True}) == []


def test_hard_policy_filters_budget_concurrency_and_capability():
    transport = StaticOmniRouteTransport(
        {"data": [{"id": "p/model", "provider": "p", "capabilities": ["tools"]}]},
        {
            "p/model": {
                "observed_at": NOW.isoformat(),
                "health": "healthy",
                "cost": 2,
                "concurrency": 4,
            }
        },
    )
    report = OmniRouteAvailabilityAdapter(transport).evaluate(
        CandidateRequirements(
            required=frozenset({"vision"}), budget_remaining=1, max_concurrency=4
        ),
        now=NOW,
    )
    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.POLICY_DENIED


def test_stale_runtime_is_hard_filtered_but_explicit_unknown_policy_is_opt_in():
    transport = StaticOmniRouteTransport(
        {"data": [{"id": "p/model", "provider": "p"}]},
        {
            "p/model": {
                "observed_at": "2026-07-16T11:58:00Z",
                "ttl_seconds": 60,
                "health": "healthy",
            }
        },
    )
    report = OmniRouteAvailabilityAdapter(transport).evaluate(now=NOW)
    assert report.candidates[0].state is AvailabilityState.UNKNOWN
    assert report.eligible == ()
    opted_in = OmniRouteAvailabilityAdapter(transport).evaluate(
        CandidateRequirements(unknown_is_eligible=True), now=NOW
    )
    assert opted_in.eligible == ()  # stale remains ineligible regardless of opt-in
