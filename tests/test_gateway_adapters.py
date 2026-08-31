"""Tests for provider-neutral gateway adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from verdict.gateway_adapter_runtime import (
    AdapterFailureSignal,
    AdapterResponseMetadata,
    NormalizedFailure,
    RouteIdentityAttestation,
    validate_telemetry,
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
from verdict.gateway_conformance import run_conformance

DIGEST = "sha256:" + "a" * 64


def manifest(**overrides: object) -> AdapterManifest:
    values: dict[str, object] = {
        "adapter_id": "fixture.openai",
        "adapter_version": "1.2.3",
        "protocol": "openai.responses",
        "protocol_version": "2025-06-18",
        "capabilities": {
            capability: CapabilitySupport.SUPPORTED for capability in AdapterCapability
        },
        "discovery": AdapterDiscoveryMetadata(
            distribution="fixture-adapter",
            entrypoint="fixture:create",
            implementation_digest=DIGEST,
        ),
        "telemetry_allowlist": ("request_id", "route_key", "status_code"),
    }
    values.update(overrides)
    return AdapterManifest(**values)


def route(model_id: str = "provider/model") -> AdapterRouteIdentity:
    return AdapterRouteIdentity(
        gateway_id="gateway-1",
        route_id="route-1",
        provider="provider",
        model_id=model_id,
        protocol="openai.responses",
    )


class FixtureAdapter:
    def __init__(
        self, current: AdapterManifest | None = None, *, actual: AdapterRouteIdentity | None = None
    ) -> None:
        self.manifest = current or manifest()
        self.actual = actual or route()

    def discover(self) -> tuple[AdapterRouteIdentity, ...]:
        return (route(),)

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
        return RouteIdentityAttestation(
            request_id=request.request_id,
            requested_alias=request.requested_alias,
            resolved_route=route(),
            actual_route=self.actual,
            source="fixture-attestation",
        )

    def cancel(self, request_id: str) -> bool:
        return bool(request_id)

    def normalize_failure(self, signal: AdapterFailureSignal) -> NormalizedFailure:
        return NormalizedFailure(
            NormalizedFailureClass.TIMEOUT if signal.timed_out else NormalizedFailureClass.UPSTREAM,
            retryable=signal.timed_out,
            status_code=signal.status_code,
        )

    def telemetry(self, request_id: str) -> dict[str, str]:
        return {"request_id": request_id}


def test_manifest_route_and_request_round_trips_are_canonical() -> None:
    item = manifest()
    assert AdapterManifest.from_dict(item.to_dict()) == item
    assert item.digest == AdapterManifest.from_dict(item.to_dict()).digest

    request = AdapterRequest(
        request_id="req-1",
        protocol=item.protocol,
        requested_alias="provider/model",
        payload={"input": "hello"},
    )
    translated = TranslatedRequest(
        request_id=request.request_id,
        protocol=request.protocol,
        requested_alias=request.requested_alias,
        payload=request.payload,
        stream=False,
    )
    assert AdapterRequest.from_dict(request.to_dict()) == request
    assert TranslatedRequest.from_dict(translated.to_dict()) == translated

    attestation = RouteIdentityAttestation(
        request_id=request.request_id,
        requested_alias=request.requested_alias,
        resolved_route=route(),
        actual_route=route(),
        source="fixture",
    )
    assert RouteIdentityAttestation.from_dict(attestation.to_dict()) == attestation


def test_capability_negotiation_is_explicit_and_fail_closed() -> None:
    item = manifest(
        capabilities={
            AdapterCapability.DISCOVERY: CapabilitySupport.SUPPORTED,
            AdapterCapability.REQUEST_TRANSLATION: CapabilitySupport.SUPPORTED,
            AdapterCapability.FAILURE_NORMALIZATION: CapabilitySupport.UNKNOWN,
        }
    )

    result = item.negotiate(
        {AdapterCapability.DISCOVERY, AdapterCapability.STREAMING, "not-a-capability"}
    )

    assert result.supported == ("discovery",)
    assert result.admitted is False
    assert result.unavailable == ("not-a-capability", "streaming")


def test_contract_rejects_unknown_and_secret_bearing_fields() -> None:
    payload = manifest().to_dict()
    payload["unexpected"] = True
    with pytest.raises(GatewayAdapterError, match="unknown field"):
        AdapterManifest.from_dict(payload)

    with pytest.raises(GatewayAdapterError, match="secret-bearing"):
        AdapterRequest(
            request_id="req-1",
            protocol="openai.responses",
            requested_alias="provider/model",
            payload={"api_key": "must-not-enter-contract"},
        )


def test_telemetry_is_allowlisted_and_secret_free() -> None:
    item = manifest()
    assert validate_telemetry(item, {"status_code": 200}) == {"status_code": 200}

    with pytest.raises(GatewayAdapterError, match="not allowed"):
        validate_telemetry(item, {"latency_ms": 2.0})
    with pytest.raises(GatewayAdapterError, match="secret-bearing"):
        validate_telemetry(item, {"request_id": "r", "authorization": "secret"})


def test_conformance_runner_accepts_valid_adapter_and_reports_digest() -> None:
    result = run_conformance(FixtureAdapter())

    assert result.passed is True
    assert {check.name for check in result.checks} == {
        "manifest",
        "discovery",
        "translation",
        "attestation",
        "streaming",
        "cancellation",
        "failure_normalization",
        "telemetry",
    }
    assert result.digest.startswith("sha256:")


def test_conformance_runner_catches_route_attestation_mismatch() -> None:
    result = run_conformance(FixtureAdapter(actual=route("different/model")))

    assert result.passed is False
    attestation = next(check for check in result.checks if check.name == "attestation")
    assert attestation.passed is False


def test_manifest_schema_accepts_canonical_payload() -> None:
    schema_path = Path(__file__).parents[1] / "verdict" / "schemas" / "gateway-adapter.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(manifest().to_dict())) == []


def test_optional_gateway_facets_are_negotiated_capabilities() -> None:
    """FR-038: abilities beyond the OpenAI baseline are declared, not assumed.

    Session memory, free-tier catalogs, and provider statistics exist on some gateways
    (OmniRoute) and not others. They must resolve to supported/unsupported/unknown so a
    gateway exposing none of them still works.
    """
    for name in ("session_memory", "free_tier_catalog", "provider_stats"):
        assert AdapterCapability(name)


def test_unsupported_optional_facet_is_not_admitted_and_does_not_raise() -> None:
    """A gateway declaring an optional facet unsupported must simply skip it."""
    current = manifest(
        capabilities={
            **{capability: CapabilitySupport.SUPPORTED for capability in AdapterCapability},
            AdapterCapability.SESSION_MEMORY: CapabilitySupport.UNSUPPORTED,
        }
    )
    negotiated = current.negotiate([AdapterCapability.SESSION_MEMORY])
    assert negotiated.supported == ()
    assert negotiated.unavailable == ("session_memory",)
    assert negotiated.admitted is False


def test_optional_facet_absent_from_manifest_reads_unknown_not_supported() -> None:
    """A silent manifest means UNKNOWN. Verdict must never assume an ability exists."""
    baseline = manifest(
        capabilities={
            capability: CapabilitySupport.SUPPORTED
            for capability in AdapterCapability
            if capability is not AdapterCapability.FREE_TIER_CATALOG
        }
    )
    assert (
        baseline.capability_status(AdapterCapability.FREE_TIER_CATALOG) is CapabilitySupport.UNKNOWN
    )
