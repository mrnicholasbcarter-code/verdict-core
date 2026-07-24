from datetime import datetime, timezone

import pytest

from verdict.availability import (
    AvailabilityState,
    CallableOmniRouteTransport,
    CandidateRequirements,
    MappingOmniRouteTransport,
    OmniRouteAvailabilityAdapter,
    OmniRouteTransportUnsupported,
    RuntimeObservation,
    StaticOmniRouteTransport,
    discover_transport_capabilities,
    explain_candidates,
    normalize_catalog,
    normalize_observation,
)
from verdict.models import ModelInfo
from verdict.planner import StructuredPlanner

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
MODEL = ModelInfo(
    id="p/model", provider="p", model="model", capability_tier=1, capabilities=["tools"]
)


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


def test_missing_observation_time_never_becomes_ready() -> None:
    state = normalize_observation(
        MODEL, RuntimeObservation(source="fixture", health="healthy", eligible=True), now=NOW
    )

    assert state.state is AvailabilityState.UNKNOWN
    assert state.reasons == ("observation timestamp missing",)


@pytest.mark.parametrize(
    "runtime",
    [
        {"observed_at": NOW.isoformat(), "health": "healthy", "ttl_seconds": "invalid"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "ttl_seconds": float("inf")},
        {"observed_at": NOW.isoformat(), "health": "healthy", "eligible": "yes"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "concurrency": "many"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "max_concurrency": False},
        {"observed_at": NOW.isoformat(), "health": "banana"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "auth": "banana"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "circuit": "banana"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "quota_remaining_pct": float("nan")},
        {"observed_at": NOW.isoformat(), "health": "healthy", "cost": 10**400},
        {"observed_at": NOW.isoformat(), "health": "healthy", "cooldown_until": "not-a-time"},
        {"observed_at": NOW.isoformat(), "health": "healthy", "lockout_until": "not-a-time"},
    ],
)
def test_malformed_runtime_scalars_fail_closed_without_raising(runtime) -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]}, {"p/model": runtime}
        )
    ).evaluate(now=NOW)

    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.MALFORMED
    assert report.candidates[0].reasons == ("malformed runtime observation",)


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


def test_mapping_transport_supports_documented_alias_operations_and_capability_overlay():
    transport = MappingOmniRouteTransport(
        {
            "list_models": {"data": [{"id": "p/model", "provider": "p"}]},
            "get_runtime": {"p/model": {"observed_at": NOW.isoformat(), "health": "healthy"}},
            "discover_capabilities": {"p/model": ["vision", "tools"]},
        }
    )
    report = OmniRouteAvailabilityAdapter(transport).evaluate(now=NOW)
    assert report.eligible[0].model.capabilities == frozenset({"vision", "tools"})
    assert report.candidates[0].state is AvailabilityState.READY


def test_catalog_uses_openai_owned_by_and_context_length_fields():
    models = normalize_catalog(
        {
            "data": [
                {"id": "bare-model-id", "owned_by": "omniroute-provider", "context_length": 131_072}
            ]
        }
    )

    assert models[0].provider == "omniroute-provider"
    assert models[0].context_window == 131_072


def test_callable_transport_discovers_supported_operations_without_failing_closed():
    transport = CallableOmniRouteTransport(
        catalog=lambda: {"data": [{"id": "p/model", "provider": "p"}]},
        runtime=lambda: {"p/model": {"observed_at": NOW.isoformat(), "health": "healthy"}},
    )
    assert discover_transport_capabilities(transport) == frozenset({"catalog", "runtime"})
    report = OmniRouteAvailabilityAdapter(transport).evaluate(now=NOW)
    assert report.eligible[0].model.id == "p/model"


def test_transport_discovery_reports_only_configured_callable_and_mapping_operations():
    callable_transport = CallableOmniRouteTransport(catalog=lambda: {"data": []})
    mapping_transport = MappingOmniRouteTransport({"list_models": {"data": []}})

    assert discover_transport_capabilities(callable_transport) == frozenset({"catalog"})
    assert discover_transport_capabilities(mapping_transport) == frozenset({"catalog"})


def test_transport_capability_discovery_ignores_unknown_advertised_operations():
    transport = StaticOmniRouteTransport(
        {"data": [{"id": "p/model", "provider": "p"}]},
        {"p/model": {"observed_at": NOW.isoformat(), "health": "healthy"}},
        capabilities={"operations": ["catalog", "runtime", "delete_everything"]},
    )
    assert discover_transport_capabilities(transport) == frozenset({"catalog", "runtime"})


def test_mapping_transport_rejects_unsupported_operation_names():
    with pytest.raises(OmniRouteTransportUnsupported):
        MappingOmniRouteTransport({"nope": {}})


def test_missing_runtime_operation_is_reported_as_typed_transport_error():
    report = OmniRouteAvailabilityAdapter(
        MappingOmniRouteTransport({"catalog": {"data": [{"id": "p/model", "provider": "p"}]}})
    ).evaluate(now=NOW)
    assert report.errors == ("runtime: expected one of runtime, get_runtime",)
    assert report.eligible == ()


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


def test_degraded_candidate_marked_ineligible_is_never_selectable() -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(observed_at=NOW, source="fixture", health="degraded", eligible=False),
        now=NOW,
    )

    assert state.state is AvailabilityState.DENIED
    assert (
        OmniRouteAvailabilityAdapter(
            StaticOmniRouteTransport(
                {"data": [{"id": "p/model", "provider": "p"}]},
                {
                    "p/model": {
                        "observed_at": NOW.isoformat(),
                        "source": "fixture",
                        "health": "degraded",
                        "eligible": False,
                    }
                },
            )
        )
        .evaluate(CandidateRequirements(allow_degraded=True), now=NOW)
        .eligible
        == ()
    )


def test_probe_hint_cannot_override_authentication_failure() -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="degraded",
            auth="unauthorized",
            eligible=False,
            raw={"probe_status": "usage_unavailable"},
        ),
        now=NOW,
    )

    assert state.state is AvailabilityState.UNAUTHORIZED


def test_stronger_exclusion_is_not_overwritten_by_policy_capacity_gate() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "health": "healthy",
                    "auth": "unauthorized",
                    "cost": 2,
                }
            },
        )
    ).evaluate(CandidateRequirements(budget_remaining=1), now=NOW)

    assert report.candidates[0].state is AvailabilityState.UNAUTHORIZED
    assert report.candidates[0].reasons == ("auth: unauthorized",)


def test_contradictory_quota_and_headroom_uses_conservative_minimum() -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="fixture",
            health="healthy",
            quota_remaining_pct=80,
            headroom_pct=0,
        ),
        now=NOW,
    )

    assert state.state is AvailabilityState.QUOTA_EXHAUSTED


@pytest.mark.parametrize(("quota", "headroom"), [(101, 50), (50, 101), (-1, 50), (50, -1)])
def test_each_quota_and_headroom_signal_must_be_in_range(quota, headroom) -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="fixture",
            health="healthy",
            quota_remaining_pct=quota,
            headroom_pct=headroom,
        ),
        now=NOW,
    )

    assert state.state is AvailabilityState.MALFORMED


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            RuntimeObservation(
                observed_at=NOW, source="fixture", health="healthy", cost=2, budget_remaining=1
            ),
            "budget headroom exceeded",
        ),
        (
            RuntimeObservation(
                observed_at=NOW,
                source="fixture",
                health="healthy",
                concurrency=4,
                max_concurrency=4,
            ),
            "concurrency limit reached",
        ),
    ],
)
def test_runtime_capacity_evidence_is_a_hard_gate(observation, reason) -> None:
    state = normalize_observation(MODEL, observation, now=NOW)

    assert state.state is AvailabilityState.POLICY_DENIED
    assert state.reasons == (reason,)


def test_estimated_request_tokens_must_fit_runtime_headroom() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "health": "healthy",
                    "token_headroom": 1_000,
                }
            },
        )
    ).evaluate(CandidateRequirements(estimated_tokens=1_001), now=NOW)

    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.POLICY_DENIED
    assert report.candidates[0].reasons == ("token headroom exceeded",)


def test_tokens_remaining_alias_is_normalized_as_request_headroom() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "health": "healthy",
                    "tokens_remaining": 1_000,
                }
            },
        )
    ).evaluate(CandidateRequirements(estimated_tokens=1_001), now=NOW)

    assert report.candidates[0].state is AvailabilityState.POLICY_DENIED
    assert report.candidates[0].reasons == ("token headroom exceeded",)


def test_planner_and_catalog_share_canonical_tool_capability() -> None:
    planner = StructuredPlanner()
    task = planner.plan("Implement a feature").task_spec
    requirements = planner.availability_requirements(task)
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p", "capabilities": ["tool-calling"]}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "health": "healthy",
                    "token_headroom": 10_000,
                    "budget_remaining": 10.0,
                }
            },
        )
    ).evaluate(requirements, now=NOW)

    assert report.candidates[0].model.capabilities == frozenset({"tools"})
    assert [candidate.model.id for candidate in report.eligible] == ["p/model"]


def test_conflicting_token_headroom_aliases_are_malformed() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "health": "healthy",
                    "token_headroom": 1_000,
                    "tokens_remaining": 1,
                }
            },
        )
    ).evaluate(CandidateRequirements(estimated_tokens=100), now=NOW)

    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.MALFORMED
    assert report.candidates[0].reasons == ("malformed runtime observation",)


def test_estimated_request_cost_must_fit_runtime_budget_headroom() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "health": "healthy",
                    "budget_remaining": 0.5,
                }
            },
        )
    ).evaluate(CandidateRequirements(estimated_cost=0.51), now=NOW)

    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.POLICY_DENIED
    assert report.candidates[0].reasons == ("budget headroom exceeded",)


def test_estimated_cost_cannot_mask_a_higher_runtime_cost() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {"p/model": {"observed_at": NOW.isoformat(), "health": "healthy", "cost": 2.0}},
        )
    ).evaluate(CandidateRequirements(budget_remaining=1.0, estimated_cost=0.5), now=NOW)

    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.POLICY_DENIED
    assert report.candidates[0].reasons == ("budget headroom exceeded",)


def test_missing_estimated_request_headroom_requires_degraded_opt_in() -> None:
    adapter = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {"p/model": {"observed_at": NOW.isoformat(), "health": "healthy"}},
        )
    )

    default = adapter.evaluate(CandidateRequirements(estimated_tokens=100), now=NOW)
    opted_in = adapter.evaluate(
        CandidateRequirements(estimated_tokens=100, allow_degraded=True), now=NOW
    )

    assert default.candidates[0].state is AvailabilityState.DEGRADED
    assert default.candidates[0].reasons == ("token headroom unknown",)
    assert default.eligible == ()
    assert [candidate.model.id for candidate in opted_in.eligible] == ["p/model"]


def test_missing_estimated_cost_headroom_fails_closed() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {"p/model": {"observed_at": NOW.isoformat(), "health": "healthy"}},
        )
    ).evaluate(CandidateRequirements(estimated_cost=0.5), now=NOW)

    assert report.candidates[0].state is AvailabilityState.DEGRADED
    assert report.candidates[0].reasons == ("budget headroom unknown",)
    assert report.eligible == ()


@pytest.mark.parametrize(
    "requirements",
    [
        {"estimated_tokens": True},
        {"estimated_tokens": 1.5},
        {"estimated_tokens": -1},
        {"estimated_cost": False},
        {"estimated_cost": -0.01},
        {"estimated_cost": float("nan")},
        {"estimated_cost": 10**400},
    ],
)
def test_invalid_request_estimates_are_rejected(requirements) -> None:
    with pytest.raises(ValueError, match="estimated"):
        CandidateRequirements(**requirements)


@pytest.mark.parametrize("token_headroom", [True, 1.5, -1, 10**400])
def test_invalid_runtime_token_headroom_is_malformed(token_headroom) -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", token_headroom=token_headroom
        ),
        now=NOW,
    )

    assert state.state is AvailabilityState.MALFORMED


@pytest.mark.parametrize(
    "observation",
    [
        RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", budget_remaining=-1
        ),
        RuntimeObservation(observed_at=NOW, source="fixture", health="healthy", cost=-1),
        RuntimeObservation(observed_at=NOW, source="fixture", health="healthy", cost=10**400),
    ],
)
def test_negative_runtime_capacity_evidence_is_malformed(observation) -> None:
    state = normalize_observation(MODEL, observation, now=NOW)

    assert state.state is AvailabilityState.MALFORMED


def test_direct_observation_is_case_normalized_and_malformed_raw_is_isolated() -> None:
    normalized = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW, source="fixture", health="Healthy", auth="Authorized", circuit="Closed"
        ),
        now=NOW,
    )
    malformed = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="degraded",
            raw=[],  # type: ignore[arg-type]
        ),
        now=NOW,
    )

    assert normalized.state is AvailabilityState.ELIGIBLE
    assert malformed.state is AvailabilityState.MALFORMED


def test_direct_malformed_error_and_probe_metadata_fail_closed() -> None:
    malformed_error = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="fixture",
            health="healthy",
            error=False,  # type: ignore[arg-type]
        ),
        now=NOW,
    )
    malformed_probe = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="healthy",
            eligible=True,
            raw={"probe_status": "not-a-real-status", "probe_availability_state": "ready"},
        ),
        now=NOW,
    )

    assert malformed_error.state is AvailabilityState.MALFORMED
    assert malformed_probe.state is AvailabilityState.MALFORMED


def test_unhashable_probe_metadata_is_failure_isolated() -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="healthy",
            eligible=True,
            raw={"probe_status": "ready", "probe_availability_state": []},
        ),
        now=NOW,
    )

    assert state.state is AvailabilityState.MALFORMED


@pytest.mark.parametrize(
    "raw",
    [
        {
            "probe_status": "ready",
            "probe_availability_state": "ready",
            "usage_available": False,
            "http_status": 200,
        },
        {"probe_status": "ready", "probe_availability_state": "ready", "usage_available": True},
        {
            "probe_status": "ready",
            "probe_availability_state": "ready",
            "usage_available": True,
            "http_status": 503,
        },
        {
            "probe_status": "ready",
            "probe_availability_state": "ready",
            "usage_available": True,
            "http_status": 200,
            "probe_error": "unexpected error",
        },
    ],
)
def test_contradictory_ready_probe_metadata_is_malformed(raw) -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="healthy",
            auth="authorized",
            eligible=True,
            raw=raw,
        ),
        now=NOW,
    )

    assert state.state is AvailabilityState.MALFORMED


@pytest.mark.parametrize(
    "raw",
    [
        {
            "probe_status": "usage_unavailable",
            "probe_availability_state": "degraded",
            "usage_available": False,
            "http_status": 401,
        },
        {
            "probe_status": "usage_unavailable",
            "probe_availability_state": "degraded",
            "usage_available": True,
            "http_status": 200,
        },
        {
            "probe_status": "completion_unavailable",
            "probe_availability_state": "degraded",
            "usage_available": True,
            "http_status": 503,
        },
        {
            "probe_status": "completion_unavailable",
            "probe_availability_state": "degraded",
            "usage_available": False,
            "http_status": 200,
        },
    ],
)
def test_contradictory_degraded_probe_metadata_is_malformed(raw) -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(observed_at=NOW, source="verdict:probe", health="degraded", raw=raw),
        now=NOW,
    )

    assert state.state is AvailabilityState.MALFORMED
    assert (
        OmniRouteAvailabilityAdapter(
            StaticOmniRouteTransport(
                {"data": [{"id": "p/model", "provider": "p"}]},
                {
                    "p/model": {
                        "observed_at": NOW.isoformat(),
                        "source": "verdict:probe",
                        "health": "degraded",
                        **raw,
                    }
                },
            )
        )
        .evaluate(CandidateRequirements(allow_degraded=True), now=NOW)
        .eligible
        == ()
    )


def test_probe_error_detail_never_leaks_through_explanations() -> None:
    state = normalize_observation(
        MODEL,
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="unhealthy",
            raw={
                "probe_status": "failed",
                "probe_availability_state": "degraded",
                "probe_error_class": "upstream_error",
                "probe_error": "Bearer secret-value https://example.test/?token=secret",
                "usage_available": False,
                "http_status": 503,
            },
        ),
        now=NOW,
    )
    explanation = explain_candidates([state], CandidateRequirements())

    assert state.state is AvailabilityState.UNAVAILABLE
    assert "secret-value" not in repr(explanation)
    assert "token=secret" not in repr(explanation)


@pytest.mark.parametrize(
    "observation",
    [
        RuntimeObservation(
            observed_at=NOW,
            source="fixture",
            health="healthy",
            error="Bearer secret-value https://example.test/?token=secret",
        ),
        RuntimeObservation(
            observed_at=NOW,
            source="verdict:probe",
            health="unhealthy",
            raw={
                "probe_status": "failed",
                "probe_availability_state": "degraded",
                "probe_error_class": "Bearer secret-value",
                "probe_error": "https://example.test/?token=secret",
                "usage_available": False,
                "http_status": 500,
            },
        ),
    ],
)
def test_untrusted_runtime_error_fields_never_leak(observation) -> None:
    state = normalize_observation(MODEL, observation, now=NOW)
    explanation = explain_candidates([state], CandidateRequirements())

    assert "secret-value" not in repr(explanation)
    assert "token=secret" not in repr(explanation)


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            RuntimeObservation(
                observed_at=NOW, source="fixture", health="unavailable", cost=2, budget_remaining=1
            ),
            AvailabilityState.UNAVAILABLE,
        ),
        (
            RuntimeObservation(
                observed_at=NOW,
                source="fixture",
                health="degraded",
                eligible=False,
                concurrency=4,
                max_concurrency=4,
            ),
            AvailabilityState.DENIED,
        ),
        (
            RuntimeObservation(
                observed_at=NOW,
                source="verdict:probe",
                health="unhealthy",
                cost=2,
                budget_remaining=1,
                raw={
                    "probe_status": "failed",
                    "probe_availability_state": "degraded",
                    "probe_error_class": "rate_limited",
                    "probe_error": "upstream returned HTTP 429",
                    "usage_available": False,
                    "http_status": 429,
                },
            ),
            AvailabilityState.RATE_LIMITED,
        ),
    ],
)
def test_intrinsic_capacity_never_overwrites_stronger_exclusions(observation, expected) -> None:
    state = normalize_observation(MODEL, observation, now=NOW)

    assert state.state is expected


@pytest.mark.parametrize(
    ("runtime", "requirements", "reason"),
    [
        (
            {"cost": 2},
            CandidateRequirements(unknown_is_eligible=True, budget_remaining=1),
            "budget exceeded",
        ),
        (
            {"concurrency": 4},
            CandidateRequirements(unknown_is_eligible=True, max_concurrency=4),
            "concurrency limit reached",
        ),
    ],
)
def test_opted_in_unknown_still_obeys_external_capacity_gates(
    runtime, requirements, reason
) -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": [{"id": "p/model", "provider": "p"}]},
            {
                "p/model": {
                    "observed_at": NOW.isoformat(),
                    "source": "fixture",
                    "health": "unknown",
                    **runtime,
                }
            },
        )
    ).evaluate(requirements, now=NOW)

    assert report.eligible == ()
    assert report.candidates[0].state is AvailabilityState.POLICY_DENIED
    assert report.candidates[0].reasons == (reason,)
