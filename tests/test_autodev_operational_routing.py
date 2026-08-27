"""Focused contracts for the US1 operational route-selection slice.

These tests intentionally describe the smallest adapter-owned surface needed by
``verdict autodev``.  They use the existing gateway response/failure contracts
and keep Verdict's policy decision separate from advisory ranking.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.autodev_routing import CandidateEvidence
from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.gateway_adapter_runtime import (
    AdapterFailureSignal,
    AdapterResponseMetadata,
    NormalizedFailure,
    RouteIdentityAttestation,
)
from verdict.gateway_adapters import (
    AdapterCapability,
    AdapterDiscoveryMetadata,
    AdapterManifest,
    AdapterRequest,
    AdapterRouteIdentity,
    CapabilitySupport,
    NormalizedFailureClass,
    TranslatedRequest,
)
from verdict.models import ModelInfo
from verdict.probes import ProbeObservation
from verdict.transitions import ByteState, RetrySafety

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def _routing() -> Any:
    """Load the prospective adapter lazily so every red test has one clear cause."""
    import importlib

    return importlib.import_module("verdict.autodev_routing")


def _route(model_id: str = "alt/model", *, route_id: str = "route-alt") -> AdapterRouteIdentity:
    return AdapterRouteIdentity(
        gateway_id="gateway-fixture",
        route_id=route_id,
        provider="alternative",
        model_id=model_id,
        protocol="openai.chat",
    )


class _Adapter:
    """A tiny gateway fixture using the public adapter contracts."""

    manifest = AdapterManifest(
        adapter_id="fixture.openai",
        adapter_version="1.0.0",
        protocol="openai.chat",
        protocol_version="2025-06-18",
        capabilities={
            AdapterCapability.DISCOVERY: CapabilitySupport.SUPPORTED,
            AdapterCapability.ROUTE_ATTESTATION: CapabilitySupport.SUPPORTED,
            AdapterCapability.FAILURE_NORMALIZATION: CapabilitySupport.SUPPORTED,
        },
        discovery=AdapterDiscoveryMetadata(
            distribution="fixture-adapter",
            entrypoint="fixture:create",
            implementation_digest=DIGEST,
        ),
    )

    def __init__(self) -> None:
        self.routes = (_route(), _route("auto/best-free", route_id="opaque-auto"))

    def discover(self) -> tuple[AdapterRouteIdentity, ...]:
        return self.routes

    def translate(self, request: AdapterRequest) -> TranslatedRequest:
        return TranslatedRequest(
            request_id=request.request_id,
            protocol=request.protocol,
            requested_alias=request.requested_alias,
            payload=request.payload,
            stream=request.stream,
        )

    def attest(
        self, request: TranslatedRequest, response: AdapterResponseMetadata
    ) -> RouteIdentityAttestation:
        actual = _route("alternative/served-v2", route_id="served-route")
        return RouteIdentityAttestation(
            request_id=request.request_id,
            requested_alias=request.requested_alias,
            resolved_route=_route(),
            actual_route=actual,
            source="fixture-response-metadata",
        )

    def normalize_failure(self, signal: AdapterFailureSignal) -> NormalizedFailure:
        if signal.status_code == 429:
            return NormalizedFailure(
                NormalizedFailureClass.RATE_LIMIT, retryable=True, status_code=429
            )
        if signal.timed_out:
            return NormalizedFailure(NormalizedFailureClass.TIMEOUT, retryable=True)
        return NormalizedFailure(
            NormalizedFailureClass.AUTHENTICATION, retryable=False, status_code=signal.status_code
        )


def _evidence(**overrides: object) -> Any:
    routing = _routing()
    values: dict[str, object] = {
        "requested_alias": "alternative/alias",
        "route": _route(),
        "availability": AvailabilityState.ELIGIBLE,
        "capabilities": {"tools": "observed", "chat": "observed"},
        "observed_at": NOW,
        "ttl_seconds": 60,
        "source": "fixture-probe",
        "quota_remaining_pct": None,
        "headroom_pct": None,
    }
    values.update(overrides)
    return routing.CandidateEvidence(**values)


def _record_ranked(ranked: list[str], candidate: Any) -> float:
    ranked.append(candidate.requested_alias)
    return 1.0


def test_discovery_returns_inspectable_concrete_routes_and_rejects_opaque_auto_routes() -> None:
    routes = _routing().discover_concrete_routes(_Adapter())

    assert tuple(route.route_id for route in routes) == ("route-alt",)
    assert all(not route.model_id.startswith("auto/") for route in routes)
    assert routes[0].gateway_id == "gateway-fixture"
    assert routes[0].provider == "alternative"


def test_candidate_evidence_preserves_observed_capability_and_freshness() -> None:
    evidence = _evidence()

    assert evidence.capability_status("tools") == "observed"
    assert evidence.is_fresh(NOW + timedelta(seconds=59)) is True
    assert evidence.is_fresh(NOW + timedelta(seconds=61)) is False
    assert evidence.evidence_source == "fixture-probe"


def test_admission_record_emits_primary_role_from_capability_not_brand() -> None:
    branded = _evidence(
        requested_alias="cc/claude-sonnet-5",
        route=_route("cc/claude-sonnet-5", route_id="branded"),
        capabilities={"tools": "observed"},
    )
    primary = _evidence(
        requested_alias="alt/subscription",
        route=_route("alt/subscription", route_id="primary-route"),
        capabilities={"tools": "observed", "primary_subscription": "observed"},
    )

    branded_record = branded.to_admission_record(admitted=True)
    primary_record = primary.to_admission_record(admitted=True)

    assert branded_record["primary"] is False
    assert branded_record["requested_identity"] == "cc/claude-sonnet-5"
    assert branded_record["evidence_digest"].startswith("sha256:")
    assert primary_record["primary"] is True
    assert primary_record["requested_identity"] == "alt/subscription"

    from verdict.autodev_run import _route_is_admitted, designated_primary_fallback

    assert _route_is_admitted(branded_record, fallback=True) is False
    assert _route_is_admitted(primary_record, fallback=True) is True

    designated = designated_primary_fallback(
        "alt/subscription",
        evidence_digest=primary_record["evidence_digest"],
        actual_identity="alt/subscription-served",
    )
    assert designated["primary"] is True
    assert designated["requested_identity"] == "alt/subscription"
    assert _route_is_admitted(designated, fallback=True) is True


def test_requested_alias_is_distinct_from_resolved_and_actual_route_identity() -> None:
    actual = _route("alternative/served-v2", route_id="served-route")
    evidence = _evidence(route=_route(), actual_route=actual)

    assert evidence.requested_alias == "alternative/alias"
    assert evidence.route.model_id == "alt/model"
    assert evidence.actual_route is not None
    assert evidence.actual_route.model_id == "alternative/served-v2"
    assert evidence.actual_route.route_id != evidence.route.route_id


def test_missing_quota_and_headroom_are_explicit_unknown_values() -> None:
    evidence = _evidence()

    assert evidence.quota_remaining_pct is None
    assert evidence.headroom_pct is None
    serialized = evidence.to_dict()
    assert serialized["quota_remaining_pct"] is None
    assert serialized["headroom_pct"] is None
    assert 100.0 not in (serialized["quota_remaining_pct"], serialized["headroom_pct"])


def test_eligibility_is_applied_before_advisory_ranking() -> None:
    rejected = _evidence(
        requested_alias="alternative/quota-exhausted",
        route=_route("alt/expensive", route_id="route-denied"),
        availability=AvailabilityState.QUOTA_EXHAUSTED,
    )
    admitted = _evidence(
        requested_alias="alternative/ready", route=_route("alt/ready", route_id="route-ready")
    )
    ranked: list[str] = []

    selection = _routing().select_eligible_route(
        (rejected, admitted),
        ranker=lambda candidate: _record_ranked(ranked, candidate),
        protected=True,
    )

    assert selection.selected.requested_alias == "alternative/ready"
    assert ranked == ["alternative/ready"]
    assert "alternative/quota-exhausted" in selection.exclusion_reasons


def test_retry_safety_requires_idempotency_and_pre_byte_state() -> None:
    routing = _routing()
    rate_limited = NormalizedFailure(
        NormalizedFailureClass.RATE_LIMIT, retryable=True, status_code=429
    )

    safe = routing.normalize_retry_safety(
        rate_limited, idempotency_key="idem-1", byte_state=ByteState.PRE_BYTES
    )
    unsafe_without_key = routing.normalize_retry_safety(
        rate_limited, idempotency_key=None, byte_state=ByteState.PRE_BYTES
    )
    unsafe_after_bytes = routing.normalize_retry_safety(
        rate_limited, idempotency_key="idem-1", byte_state=ByteState.BYTES_EMITTED
    )

    assert safe.retryable is True
    assert safe.safety is RetrySafety.SAFE
    assert unsafe_without_key.retryable is False
    assert unsafe_without_key.safety is RetrySafety.UNKNOWN
    assert unsafe_after_bytes.retryable is False
    assert unsafe_after_bytes.safety is RetrySafety.UNSAFE


def test_non_transient_failure_is_never_normalized_as_retryable() -> None:
    failure = NormalizedFailure(
        NormalizedFailureClass.AUTHENTICATION, retryable=False, status_code=401
    )

    result = _routing().normalize_retry_safety(
        failure, idempotency_key="idem-1", byte_state=ByteState.PRE_BYTES
    )

    assert result.retryable is False
    assert result.safety is RetrySafety.UNSAFE


def _report(*candidates: AvailabilityCandidate) -> AvailabilityReport:
    return AvailabilityReport(
        candidates=tuple(candidates),
        eligible=tuple(
            candidate for candidate in candidates if candidate.state is AvailabilityState.ELIGIBLE
        ),
        source="omniroute",
        freshness_seconds=2.0,
    )


def test_composes_candidate_evidence_from_availability_report_without_inventing_optional_facets() -> (
    None
):
    candidate = AvailabilityCandidate(
        model=ModelInfo(
            id="alternative/ready",
            provider="alternative",
            capabilities=frozenset({"tools", "chat"}),
        ),
        state=AvailabilityState.ELIGIBLE,
        source="omniroute-runtime",
        freshness_seconds=2.0,
        headroom_pct=None,
        normalized={"quota_remaining_pct": None},
    )
    route = _route("alternative/ready", route_id="route-ready")

    evidence = _routing().compose_candidate_evidence(
        _report(candidate),
        {"alternative/ready": route},
        requested_alias="alternative/alias",
        observed_at=NOW,
        ttl_seconds=60,
    )

    assert len(evidence) == 1
    assert evidence[0].requested_alias == "alternative/alias"
    assert evidence[0].route == route
    assert evidence[0].availability is AvailabilityState.ELIGIBLE
    assert evidence[0].source == "omniroute-runtime"
    assert evidence[0].freshness_seconds == 2.0
    assert evidence[0].capabilities == {"chat": "observed", "tools": "observed"}
    assert evidence[0].quota_remaining_pct is None
    assert evidence[0].headroom_pct is None


def test_attests_requested_resolved_and_actual_identity_through_adapter_contract() -> None:
    adapter = _Adapter()
    request = AdapterRequest(
        request_id="request-1",
        protocol="openai.chat",
        requested_alias="alternative/alias",
        payload={"messages": []},
    )
    translated = adapter.translate(request)
    response = AdapterResponseMetadata("request-1", {"model": "alternative/served-v2"})

    attestation = _routing().attest_response(adapter, translated, response)

    assert attestation.requested_alias == "alternative/alias"
    assert attestation.resolved_route.model_id == "alt/model"
    assert attestation.actual_route is not None
    assert attestation.actual_route.model_id == "alternative/served-v2"


def test_selection_uses_existing_eligibility_gate_before_ranker() -> None:
    rejected = _evidence(
        requested_alias="alternative/quota-exhausted",
        route=_route("alternative/quota-exhausted", route_id="route-denied"),
        availability=AvailabilityState.QUOTA_EXHAUSTED,
    )
    admitted = _evidence(
        requested_alias="alternative/ready",
        route=_route("alternative/ready", route_id="route-ready"),
    )
    ranked: list[str] = []

    selection = _routing().select_eligible_route(
        (rejected, admitted),
        ranker=lambda candidate: _record_ranked(ranked, candidate),
        protected=True,
    )

    assert selection.selected is admitted
    assert ranked == ["alternative/ready"]


class _AvailabilitySurface:
    """Existing availability/health/probe surface consumed by the live adapter."""

    def __init__(self, report: AvailabilityReport) -> None:
        self.report = report
        self.calls = 0

    def evaluate(self, *, now: datetime | None = None) -> AvailabilityReport:
        assert now == NOW
        self.calls += 1
        return self.report


def _probe(model_id: str = "alternative/ready") -> ProbeObservation:
    return ProbeObservation(
        model_id=model_id,
        availability_state="ready",
        status="ready",
        observed_at=NOW,
        latency_ms=12.5,
        usage_available=True,
        prompt_tokens=3,
        completion_tokens=1,
        total_tokens=4,
        http_status=200,
    )


def test_live_adapter_discovers_concrete_routes_from_existing_availability_surface() -> None:
    candidate = AvailabilityCandidate(
        model=ModelInfo(
            id="alternative/ready", provider="alternative", capabilities=frozenset({"chat"})
        ),
        state=AvailabilityState.ELIGIBLE,
        source="omniroute-runtime",
        freshness_seconds=2.0,
        normalized={"quota_remaining_pct": None},
    )
    surface = _AvailabilitySurface(_report(candidate))
    adapter = _routing().OpenAICompatibleEvidenceAdapter(
        surface, gateway_id="omniroute-local", protocol="openai.chat", ttl_seconds=60
    )

    evidence = adapter.observe(
        requested_alias="alternative/ready", observed_at=NOW, probes={"alternative/ready": _probe()}
    )

    assert surface.calls == 1
    assert len(evidence) == 1
    assert evidence[0].route == AdapterRouteIdentity(
        gateway_id="omniroute-local",
        route_id="alternative/ready",
        provider="alternative",
        model_id="alternative/ready",
        protocol="openai.chat",
    )
    assert evidence[0].capability_status("chat") == "observed"
    assert evidence[0].quota_remaining_pct is None
    assert evidence[0].headroom_pct is None


def test_live_adapter_attests_only_actual_identity_present_in_response_metadata() -> None:
    candidate = AvailabilityCandidate(
        model=ModelInfo(id="alternative/ready", provider="alternative"),
        state=AvailabilityState.ELIGIBLE,
        source="omniroute-runtime",
    )
    adapter = _routing().OpenAICompatibleEvidenceAdapter(
        _AvailabilitySurface(_report(candidate)),
        gateway_id="omniroute-local",
        protocol="openai.chat",
    )
    adapter.observe(requested_alias="alternative/ready", observed_at=NOW)
    request = adapter.translate(
        AdapterRequest(
            request_id="request-live-1",
            protocol="openai.chat",
            requested_alias="alternative/ready",
            payload={"messages": []},
        )
    )

    observed = adapter.attest(
        request,
        AdapterResponseMetadata(
            "request-live-1",
            {
                "actual_route": {
                    "gateway_id": "omniroute-local",
                    "route_id": "connection-7",
                    "provider": "alternative-upstream",
                    "model_id": "alternative/served-v2",
                    "protocol": "openai.chat",
                }
            },
        ),
    )
    unavailable = adapter.attest(
        request, AdapterResponseMetadata("request-live-1", {"request_id": "upstream-request-9"})
    )

    assert observed.resolved_route.model_id == "alternative/ready"
    assert observed.actual_route is not None
    assert observed.actual_route.route_id == "connection-7"
    assert observed.actual_route.model_id == "alternative/served-v2"
    assert unavailable.actual_route is None
    assert unavailable.source == "response-metadata:actual-route-unavailable"


def test_live_adapter_normalizes_failures_without_retrying_authentication() -> None:
    adapter = _routing().OpenAICompatibleEvidenceAdapter(
        _AvailabilitySurface(_report()), gateway_id="omniroute-local"
    )

    rate_limit = adapter.normalize_failure(AdapterFailureSignal("http", status_code=429))
    timeout = adapter.normalize_failure(AdapterFailureSignal("timeout", timed_out=True))
    auth = adapter.normalize_failure(AdapterFailureSignal("http", status_code=401))

    assert rate_limit == NormalizedFailure(
        NormalizedFailureClass.RATE_LIMIT, retryable=True, status_code=429
    )
    assert timeout == NormalizedFailure(NormalizedFailureClass.TIMEOUT, retryable=True)
    assert auth == NormalizedFailure(
        NormalizedFailureClass.AUTHENTICATION, retryable=False, status_code=401
    )


# --- Clarified requirements (AC-0.10): qualification is fresh source-linked
# capability evidence only; name/tier/reputation/self-report never qualify. ---
def test_qualification_rejects_reputation_and_self_report_without_evidence() -> None:
    from verdict.autodev_run import worker_capability_report

    now = datetime.now(timezone.utc)
    # A candidate carrying only identity attributes has no qualification.
    named = {"handoff_to": "big/tier", "reputation": 0.99, "tier": "frontier"}
    report = worker_capability_report(["patch"], named, {}, now)
    assert report["qualified"] is False
    assert "patch" in report["unsatisfied_capabilities"]
    # Rejected non-evidence inputs are recorded as checked-and-rejected.
    assert any("rejected_input" in item for item in report["evidence_checked"])


def test_qualification_requires_every_transition_capability_fresh() -> None:
    from verdict.autodev_run import worker_capability_report

    now = NOW
    capable_stale = CandidateEvidence(
        requested_alias="alt/stale",
        route=_route("alt/stale"),
        availability=AvailabilityState.ELIGIBLE,
        capabilities={"patch": "observed", "test": "observed"},
        observed_at=NOW - timedelta(seconds=3600),
        ttl_seconds=60,
        source="fixture-probe",
    )
    report = worker_capability_report(
        ["patch", "test"], {"handoff_to": "alt/stale"}, {"alt/stale": capable_stale}, now
    )
    assert report["qualified"] is False
    assert "freshness" in report["unsatisfied_capabilities"]

    missing_one = CandidateEvidence(
        requested_alias="alt/partial",
        route=_route("alt/partial"),
        availability=AvailabilityState.ELIGIBLE,
        capabilities={"patch": "observed"},
        observed_at=NOW,
        ttl_seconds=300,
        source="fixture-probe",
    )
    report = worker_capability_report(
        ["patch", "test"], {"handoff_to": "alt/partial"}, {"alt/partial": missing_one}, now
    )
    assert report["unsatisfied_capabilities"] == ["test"]

    qualified = CandidateEvidence(
        requested_alias="alt/ok",
        route=_route("alt/ok"),
        availability=AvailabilityState.ELIGIBLE,
        capabilities={"patch": "observed", "test": "observed"},
        observed_at=NOW,
        ttl_seconds=300,
        source="fixture-probe",
    )
    report = worker_capability_report(
        ["patch", "test"], {"handoff_to": "alt/ok"}, {"alt/ok": qualified}, now
    )
    assert report["qualified"] is True
    assert report["unsatisfied_capabilities"] == []


def test_catalog_and_runtime_conflicts_are_preserved_not_merged() -> None:
    candidate = AvailabilityCandidate(
        model=ModelInfo(
            id="alternative/ready",
            provider="alternative",
            capabilities=frozenset({"chat", "tools"}),
            context_window=200_000,
        ),
        state=AvailabilityState.ELIGIBLE,
        source="omniroute-runtime",
        freshness_seconds=2.0,
        headroom_pct=None,
        normalized={
            "quota_remaining_pct": None,
            "catalog": {
                "context_window": 200_000,
                "capabilities": ["chat", "tools"],
                "freshness_seconds": 3600.0,
            },
            "runtime": {
                "context_window": 128_000,
                "capabilities": ["chat"],
                "freshness_seconds": 2.0,
            },
        },
    )
    route = _route("alternative/ready", route_id="route-ready")

    evidence = _routing().compose_candidate_evidence(
        _report(candidate),
        {"alternative/ready": route},
        requested_alias="alternative/alias",
        observed_at=NOW,
        ttl_seconds=60,
    )

    assert len(evidence) == 1
    fields = {item["field"] for item in evidence[0].conflicts}
    assert "context_window" in fields
    assert "capabilities" in fields
    context = next(item for item in evidence[0].conflicts if item["field"] == "context_window")
    assert context["catalog_value"] == 200_000
    assert context["runtime_value"] == 128_000
    assert context["catalog_freshness_seconds"] == 3600.0
    assert context["runtime_freshness_seconds"] == 2.0
    serialized = evidence[0].to_dict()
    assert serialized["conflicts"]
    assert serialized["field_freshness"]["quota_remaining_pct"] is None
    assert serialized["field_freshness"]["headroom_pct"] is None
    assert serialized["quota_remaining_pct"] is None
    assert serialized["headroom_pct"] is None


def test_openai_served_model_is_attested_distinct_from_requested_alias() -> None:
    candidate = AvailabilityCandidate(
        model=ModelInfo(id="alternative/ready", provider="alternative"),
        state=AvailabilityState.ELIGIBLE,
        source="omniroute-runtime",
    )
    adapter = _routing().OpenAICompatibleEvidenceAdapter(
        _AvailabilitySurface(_report(candidate)),
        gateway_id="omniroute-local",
        protocol="openai.chat",
    )
    adapter.observe(requested_alias="alternative/ready", observed_at=NOW)
    request = adapter.translate(
        AdapterRequest(
            request_id="request-live-served",
            protocol="openai.chat",
            requested_alias="alternative/ready",
            payload={"messages": []},
        )
    )

    attested = adapter.attest(
        request, AdapterResponseMetadata("request-live-served", {"model": "alternative/served-v2"})
    )

    assert attested.requested_alias == "alternative/ready"
    assert attested.resolved_route.model_id == "alternative/ready"
    assert attested.actual_route is not None
    assert attested.actual_route.model_id == "alternative/served-v2"
    assert attested.actual_route.model_id != attested.requested_alias
    assert attested.source == "response-metadata:served-model"
