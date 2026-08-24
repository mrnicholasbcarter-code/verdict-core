"""Thin OpenAI-compatible evidence adapter for the autodev work unit.

The adapter composes existing catalog, availability/health, and probe truth.  It
does not perform inference or duplicate gateway policy.  Rich optional facets
remain unknown unless an existing surface or the actual response supplies them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.eligibility import EligibilityGate
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
    GatewayAdapterError,
    NormalizedFailureClass,
    TranslatedRequest,
)
from verdict.probes import ProbeObservation
from verdict.transitions import ByteState, RetrySafety


class RouteDiscoveryAdapter(Protocol):
    """The portion of a gateway adapter needed for concrete discovery."""

    def discover(self) -> Sequence[AdapterRouteIdentity]: ...


class ResponseAttestationAdapter(Protocol):
    """Adapter contract for preserving identity observed in a response."""

    def attest(
        self, request: Any, response: AdapterResponseMetadata
    ) -> RouteIdentityAttestation: ...


class AvailabilitySurface(Protocol):
    """Existing availability/health/probe projection used by the live adapter."""

    def evaluate(self, *, now: datetime | None = None) -> AvailabilityReport: ...


@dataclass(frozen=True)
class CandidateEvidence:
    """Secret-free, source-bound facts about one requested route."""

    requested_alias: str
    route: AdapterRouteIdentity
    availability: AvailabilityState
    capabilities: Mapping[str, str]
    observed_at: datetime
    ttl_seconds: int
    source: str
    freshness_seconds: float | None = None
    quota_remaining_pct: float | None = None
    headroom_pct: float | None = None
    actual_route: AdapterRouteIdentity | None = None

    def __post_init__(self) -> None:
        if not self.requested_alias.strip():
            raise GatewayAdapterError("requested_alias must be non-empty")
        if not isinstance(self.route, AdapterRouteIdentity):
            raise GatewayAdapterError("route must be a route identity")
        if self.actual_route is not None and not isinstance(
            self.actual_route, AdapterRouteIdentity
        ):
            raise GatewayAdapterError("actual_route must be a route identity")
        try:
            object.__setattr__(self, "availability", AvailabilityState(self.availability))
        except ValueError as exc:
            raise GatewayAdapterError("availability is invalid") from exc
        if self.observed_at.tzinfo is None:
            raise GatewayAdapterError("observed_at must be timezone-aware")
        if type(self.ttl_seconds) is not int or self.ttl_seconds <= 0:
            raise GatewayAdapterError("ttl_seconds must be a positive integer")
        if not self.source.strip():
            raise GatewayAdapterError("source must be non-empty")
        object.__setattr__(self, "capabilities", dict(self.capabilities))
        if self.freshness_seconds is not None and (
            isinstance(self.freshness_seconds, bool) or self.freshness_seconds < 0
        ):
            raise GatewayAdapterError("freshness_seconds must be non-negative or unknown")
        for value in (self.quota_remaining_pct, self.headroom_pct):
            if value is not None and (isinstance(value, bool) or not 0 <= value <= 100):
                raise GatewayAdapterError("quota/headroom must be percentages or unknown")

    @property
    def evidence_source(self) -> str:
        return self.source

    def capability_status(self, capability: str) -> str:
        """Return observed/claimed/unknown status without inventing capability."""
        return self.capabilities.get(capability, "unknown")

    def is_fresh(self, at: datetime | None = None) -> bool:
        current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        observed = self.observed_at.astimezone(timezone.utc)
        return observed <= current <= observed + timedelta(seconds=self.ttl_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_alias": self.requested_alias,
            "route": self.route.to_dict(),
            "actual_route": None if self.actual_route is None else self.actual_route.to_dict(),
            "availability": self.availability.value,
            "capabilities": dict(sorted(self.capabilities.items())),
            "observed_at": self.observed_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "ttl_seconds": self.ttl_seconds,
            "source": self.source,
            "freshness_seconds": self.freshness_seconds,
            "quota_remaining_pct": self.quota_remaining_pct,
            "headroom_pct": self.headroom_pct,
        }


@dataclass(frozen=True)
class RouteSelection:
    """Pre-ranking admission result and advisory selection."""

    selected: CandidateEvidence
    exclusion_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    safety: RetrySafety


_TRANSIENT = frozenset(
    {
        NormalizedFailureClass.RATE_LIMIT,
        NormalizedFailureClass.QUOTA,
        NormalizedFailureClass.TRANSPORT,
        NormalizedFailureClass.TIMEOUT,
        NormalizedFailureClass.UPSTREAM,
    }
)


class OpenAICompatibleEvidenceAdapter:
    """Expose inspected OpenAI-compatible route evidence through adapter v1.

    Discovery is derived from the existing availability report, which itself is
    responsible for composing catalog, provider health, circuits, and optional
    bounded probes.  The adapter caches only that report's secret-free route
    projection so response attestation can retain requested and resolved identity.
    """

    def __init__(
        self,
        availability: AvailabilitySurface,
        *,
        gateway_id: str,
        protocol: str = "openai.chat",
        ttl_seconds: int = 60,
        adapter_id: str = "verdict.openai-compatible",
        adapter_version: str = "1.0.0",
    ) -> None:
        if not gateway_id.strip():
            raise GatewayAdapterError("gateway_id must be non-empty")
        if not protocol.strip():
            raise GatewayAdapterError("protocol must be non-empty")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise GatewayAdapterError("ttl_seconds must be a positive integer")
        self.availability = availability
        self.gateway_id = gateway_id
        self.protocol = protocol
        self.ttl_seconds = ttl_seconds
        self._routes: tuple[AdapterRouteIdentity, ...] = ()
        self.manifest = AdapterManifest(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            protocol=protocol,
            protocol_version="openai-compatible-v1",
            capabilities={
                AdapterCapability.DISCOVERY: CapabilitySupport.SUPPORTED,
                AdapterCapability.REQUEST_TRANSLATION: CapabilitySupport.SUPPORTED,
                AdapterCapability.ROUTE_ATTESTATION: CapabilitySupport.SUPPORTED,
                AdapterCapability.FAILURE_NORMALIZATION: CapabilitySupport.SUPPORTED,
                AdapterCapability.STREAMING: CapabilitySupport.UNKNOWN,
                AdapterCapability.CANCELLATION: CapabilitySupport.UNKNOWN,
                AdapterCapability.TELEMETRY: CapabilitySupport.UNKNOWN,
            },
            discovery=AdapterDiscoveryMetadata(
                distribution="verdict-core",
                entrypoint="verdict.autodev_routing:OpenAICompatibleEvidenceAdapter",
                implementation_digest="sha256:" + "0" * 64,
            ),
        )

    def observe(
        self,
        *,
        requested_alias: str,
        observed_at: datetime | None = None,
        probes: Mapping[str, ProbeObservation] | None = None,
    ) -> tuple[CandidateEvidence, ...]:
        """Read current evidence and return concrete secret-free candidates."""
        current = observed_at or datetime.now(timezone.utc)
        report = self.availability.evaluate(now=current)
        self._routes = tuple(
            _route_from_candidate(self.gateway_id, self.protocol, item)
            for item in report.candidates
            if not _opaque_route(item.model.id)
        )
        routes = {route.model_id: route for route in self._routes}
        evidence = compose_candidate_evidence(
            report,
            routes,
            requested_alias=requested_alias,
            observed_at=current,
            ttl_seconds=self.ttl_seconds,
        )
        if not probes:
            return evidence
        return tuple(_merge_probe(item, probes.get(item.route.model_id)) for item in evidence)

    def discover(self) -> tuple[AdapterRouteIdentity, ...]:
        """Return only routes already observed by :meth:`observe`."""
        return self._routes

    def translate(self, request: AdapterRequest) -> TranslatedRequest:
        if request.protocol != self.protocol:
            raise GatewayAdapterError("request protocol does not match adapter protocol")
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
        if response.request_id != request.request_id:
            raise GatewayAdapterError("response request_id does not match request")
        resolved = next(
            (
                route
                for route in self._routes
                if request.requested_alias in {route.route_id, route.model_id}
            ),
            None,
        )
        if resolved is None:
            raise GatewayAdapterError("requested alias was not discovered")
        actual = _actual_route(response.metadata)
        source = (
            "response-metadata:actual-route"
            if actual is not None
            else "response-metadata:actual-route-unavailable"
        )
        return RouteIdentityAttestation(
            request_id=request.request_id,
            requested_alias=request.requested_alias,
            resolved_route=resolved,
            actual_route=actual,
            source=source,
        )

    def normalize_failure(self, signal: AdapterFailureSignal) -> NormalizedFailure:
        status = signal.status_code
        if signal.cancelled:
            failure_class = NormalizedFailureClass.CANCELLED
        elif signal.timed_out:
            failure_class = NormalizedFailureClass.TIMEOUT
        elif status in {401, 403}:
            failure_class = NormalizedFailureClass.AUTHENTICATION
        elif status == 402:
            failure_class = NormalizedFailureClass.QUOTA
        elif status == 429:
            failure_class = NormalizedFailureClass.RATE_LIMIT
        elif status in {400, 404, 405, 409, 415, 422}:
            failure_class = NormalizedFailureClass.CAPABILITY
        elif status is not None and status >= 500:
            failure_class = NormalizedFailureClass.UPSTREAM
        elif status is None:
            failure_class = NormalizedFailureClass.TRANSPORT
        else:
            failure_class = NormalizedFailureClass.UNKNOWN
        return NormalizedFailure(
            failure_class=failure_class,
            retryable=failure_class in _TRANSIENT,
            status_code=status,
        )


def discover_concrete_routes(adapter: RouteDiscoveryAdapter) -> tuple[AdapterRouteIdentity, ...]:
    """Inspect an adapter and return only explicitly identifiable routes.

    ``auto/*`` is an opaque resolver alias, not route evidence; it is never
    allowed to enter the candidate set.
    """
    routes = adapter.discover()
    result: list[AdapterRouteIdentity] = []
    seen: set[tuple[str, str]] = set()
    for route in routes:
        if not isinstance(route, AdapterRouteIdentity):
            raise GatewayAdapterError("adapter discovery returned a non-route identity")
        if route.model_id.startswith("auto/") or route.route_id.startswith("auto/"):
            continue
        identity = (route.gateway_id, route.route_id)
        if identity not in seen:
            result.append(route)
            seen.add(identity)
    return tuple(result)


def select_eligible_route(
    candidates: Iterable[CandidateEvidence],
    *,
    ranker: Callable[[CandidateEvidence], float] | None = None,
    protected: bool = False,
) -> RouteSelection:
    """Apply hard availability eligibility, then invoke the advisory ranker."""
    candidate_list = list(candidates)
    if not candidate_list:
        raise ValueError("no eligible route; exclusions: ")

    # Keep the existing EligibilityGate as the sole hard admission authority.
    # The report is an in-memory projection of already-observed evidence; this
    # function performs no availability or network calls.
    by_model: dict[str, CandidateEvidence] = {
        candidate.route.model_id: candidate for candidate in candidate_list
    }

    def report_for(model_id: str) -> AvailabilityReport:
        candidate = by_model[model_id]
        model = _model_for_evidence(candidate)
        availability = AvailabilityCandidate(
            model=model,
            state=candidate.availability,
            source=candidate.source,
            headroom_pct=candidate.headroom_pct,
            freshness_seconds=0.0,
            normalized={
                "quota_remaining_pct": candidate.quota_remaining_pct,
                "headroom_pct": candidate.headroom_pct,
            },
        )
        return AvailabilityReport(
            candidates=(availability,),
            # EligibilityGate is the admission authority; this projection is
            # intentionally not a second policy decision.
            eligible=(),
            source=candidate.source,
            freshness_seconds=0.0,
        )

    gate = EligibilityGate(report_for, protected_fail_closed=True, allow_unverified_in_dev=False)
    result = gate.evaluate(
        [_model_for_evidence(candidate) for candidate in candidate_list],
        protected=protected,
        dev_mode=False,
    )
    admitted_models = {model.id for model in result.admitted}
    eligible = [candidate for candidate in candidate_list if candidate.route.model_id in admitted_models]
    excluded = [candidate.requested_alias for candidate in candidate_list if candidate.route.model_id not in admitted_models]
    if not eligible:
        raise ValueError("no eligible route; exclusions: " + ", ".join(excluded))
    selected = max(eligible, key=ranker or (lambda _candidate: 0.0))
    return RouteSelection(selected=selected, exclusion_reasons=tuple(excluded))


def compose_candidate_evidence(
    report: AvailabilityReport,
    routes: Mapping[str, AdapterRouteIdentity],
    *,
    requested_alias: str,
    observed_at: datetime,
    ttl_seconds: int,
) -> tuple[CandidateEvidence, ...]:
    """Compose secret-free evidence from existing availability observations.

    Optional quota/headroom values are copied only when supplied by the
    availability surface.  Missing facets remain ``None`` (unknown).
    """
    evidence: list[CandidateEvidence] = []
    for item in report.candidates:
        route = routes.get(item.model.id)
        if route is None:
            continue
        normalized = item.normalized
        capabilities = {
            str(capability): "observed" for capability in sorted(item.model.capabilities)
        }
        quota = _optional_percentage(normalized.get("quota_remaining_pct"))
        headroom = item.headroom_pct
        if headroom is None:
            headroom = _optional_percentage(normalized.get("headroom_pct"))
        evidence.append(
            CandidateEvidence(
                requested_alias=requested_alias,
                route=route,
                availability=item.state,
                capabilities=capabilities,
                observed_at=observed_at,
                ttl_seconds=ttl_seconds,
                source=item.source or report.source,
                freshness_seconds=item.freshness_seconds,
                quota_remaining_pct=quota,
                headroom_pct=headroom,
            )
        )
    return tuple(evidence)


def attest_response(
    adapter: ResponseAttestationAdapter,
    request: Any,
    response: AdapterResponseMetadata,
) -> RouteIdentityAttestation:
    """Return the adapter's response-bound requested/resolved/actual identity."""
    attestation = adapter.attest(request, response)
    if not isinstance(attestation, RouteIdentityAttestation):
        raise GatewayAdapterError("adapter returned an invalid route attestation")
    return attestation


def _route_from_candidate(
    gateway_id: str, protocol: str, candidate: AvailabilityCandidate
) -> AdapterRouteIdentity:
    return AdapterRouteIdentity(
        gateway_id=gateway_id,
        route_id=candidate.model.id,
        provider=candidate.model.provider or "unknown",
        model_id=candidate.model.id,
        protocol=protocol,
    )


def _opaque_route(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith(("auto/", "combo/", "router/", "virtual/")) or normalized in {
        "auto",
        "combo",
        "default",
        "router",
        "virtual",
    }


def _merge_probe(
    evidence: CandidateEvidence, probe: ProbeObservation | None
) -> CandidateEvidence:
    if probe is None or probe.model_id != evidence.route.model_id:
        return evidence
    capabilities = dict(evidence.capabilities)
    if probe.status == "ready" and probe.availability_state == "ready":
        capabilities["chat"] = "observed"
    return CandidateEvidence(
        requested_alias=evidence.requested_alias,
        route=evidence.route,
        availability=evidence.availability,
        capabilities=capabilities,
        observed_at=max(evidence.observed_at, probe.observed_at),
        ttl_seconds=evidence.ttl_seconds,
        source=(
            f"{evidence.source}+verdict:probe"
            if probe.status == "ready"
            else evidence.source
        ),
        freshness_seconds=evidence.freshness_seconds,
        quota_remaining_pct=evidence.quota_remaining_pct,
        headroom_pct=evidence.headroom_pct,
        actual_route=evidence.actual_route,
    )


def _actual_route(metadata: Mapping[str, Any]) -> AdapterRouteIdentity | None:
    value = metadata.get("actual_route")
    if isinstance(value, Mapping):
        try:
            return AdapterRouteIdentity.from_dict(value)
        except GatewayAdapterError:
            return None

    # Generic OpenAI bodies often expose only the served model.  That is useful
    # metadata, but it cannot prove the provider/connection/route identity, so
    # the richer actual route deliberately remains unknown.
    return None


def _model_for_evidence(candidate: CandidateEvidence) -> Any:
    from verdict.models import ModelInfo

    return ModelInfo(
        id=candidate.route.model_id,
        provider=candidate.route.provider,
        capabilities=frozenset(candidate.capabilities),
        availability_state=candidate.availability.value,
        source=candidate.source,
    )


def _optional_percentage(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and 0 <= value <= 100:
        return float(value)
    return None


def normalize_retry_safety(
    failure: NormalizedFailure,
    *,
    idempotency_key: str | None,
    byte_state: ByteState,
) -> RetryDecision:
    """Normalize retryability using failure class, idempotency, and byte state."""
    if failure.failure_class not in _TRANSIENT or not failure.retryable:
        return RetryDecision(False, RetrySafety.UNSAFE)
    if byte_state is ByteState.BYTES_EMITTED:
        return RetryDecision(False, RetrySafety.UNSAFE)
    if not idempotency_key:
        return RetryDecision(False, RetrySafety.UNKNOWN)
    return RetryDecision(True, RetrySafety.SAFE)


__all__ = [
    "CandidateEvidence",
    "OpenAICompatibleEvidenceAdapter",
    "RetryDecision",
    "RouteSelection",
    "attest_response",
    "compose_candidate_evidence",
    "discover_concrete_routes",
    "normalize_retry_safety",
    "select_eligible_route",
]
