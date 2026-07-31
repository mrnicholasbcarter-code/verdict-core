"""Hermetic conformance checks for provider-neutral gateway adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verdict.gateway_adapter_runtime import (
    AdapterFailureSignal,
    AdapterResponseMetadata,
    GatewayAdapter,
    validate_telemetry,
)
from verdict.gateway_adapters import (
    GATEWAY_ADAPTER_CONTRACT_VERSION,
    AdapterCapability,
    AdapterManifest,
    AdapterRequest,
    AdapterRouteIdentity,
    CapabilitySupport,
    GatewayAdapterError,
    NormalizedFailureClass,
    TranslatedRequest,
)


@dataclass(frozen=True)
class ConformanceCheck:
    """One deterministic adapter conformance result."""

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ConformanceReport:
    """Machine-readable report suitable for CI and compatibility matrices."""

    adapter_id: str
    adapter_version: str
    checks: tuple[ConformanceCheck, ...]
    contract_version: str = GATEWAY_ADAPTER_CONTRACT_VERSION

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }

    @property
    def digest(self) -> str:
        import hashlib
        import json

        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def run_conformance(adapter: GatewayAdapter) -> ConformanceReport:
    """Run bounded, deterministic checks without network or credentials."""

    try:
        manifest = adapter.manifest
        if not isinstance(manifest, AdapterManifest):
            raise GatewayAdapterError("manifest has the wrong type")
        AdapterManifest.from_dict(manifest.to_dict())
    except Exception as exc:
        return ConformanceReport(
            "invalid-adapter",
            "unknown",
            (ConformanceCheck("manifest", False, f"failed:{type(exc).__name__}"),),
        )

    checks = [ConformanceCheck("manifest", True, "valid")]
    request = AdapterRequest(
        "gateway-adapter-conformance-v1",
        manifest.protocol,
        "fixture/alias",
        {"messages": [{"role": "user", "content": "conformance"}]},
    )
    route: AdapterRouteIdentity | None = None
    translated: TranslatedRequest | None = None

    try:
        if not _supported(manifest, AdapterCapability.DISCOVERY):
            raise GatewayAdapterError("capability unavailable")
        routes = tuple(adapter.discover())
        route = routes[0] if routes else None
        valid = bool(routes) and all(
            isinstance(item, AdapterRouteIdentity) and item.protocol == manifest.protocol
            for item in routes
        )
        checks.append(ConformanceCheck("discovery", valid, "valid" if valid else "mismatch"))
    except Exception as exc:
        checks.append(ConformanceCheck("discovery", False, f"failed:{type(exc).__name__}"))

    try:
        if not _supported(manifest, AdapterCapability.REQUEST_TRANSLATION):
            raise GatewayAdapterError("capability unavailable")
        translated = adapter.translate(request)
        valid = (
            isinstance(translated, TranslatedRequest)
            and translated.request_id == request.request_id
            and translated.protocol == request.protocol
            and translated.requested_alias == request.requested_alias
            and not translated.stream
        )
        checks.append(ConformanceCheck("translation", valid, "valid" if valid else "mismatch"))
    except Exception as exc:
        checks.append(ConformanceCheck("translation", False, f"failed:{type(exc).__name__}"))

    _check_attestation(adapter, manifest, request, translated, route, checks)
    _check_streaming(adapter, manifest, request, checks)
    _check_cancellation(adapter, manifest, request, checks)
    _check_failures(adapter, manifest, checks)
    _check_telemetry(adapter, manifest, request, checks)
    return ConformanceReport(manifest.adapter_id, manifest.adapter_version, tuple(checks))


def run_gateway_adapter_conformance(adapter: GatewayAdapter) -> ConformanceReport:
    """Descriptive alias for :func:`run_conformance`."""

    return run_conformance(adapter)


def _supported(manifest: AdapterManifest, capability: AdapterCapability) -> bool:
    return manifest.capability_status(capability) is CapabilitySupport.SUPPORTED


def _optional(name: str, status: CapabilitySupport) -> ConformanceCheck:
    return ConformanceCheck(
        name, status is CapabilitySupport.UNSUPPORTED, f"declared:{status.value}"
    )


def _check_attestation(
    adapter: GatewayAdapter,
    manifest: AdapterManifest,
    request: AdapterRequest,
    translated: TranslatedRequest | None,
    route: AdapterRouteIdentity | None,
    checks: list[ConformanceCheck],
) -> None:
    status = manifest.capability_status(AdapterCapability.ROUTE_ATTESTATION)
    try:
        if status is not CapabilitySupport.SUPPORTED:
            checks.append(_optional("attestation", status))
            return
        if translated is None or route is None:
            raise GatewayAdapterError("translation or route unavailable")
        attestation = adapter.attest(
            translated,
            AdapterResponseMetadata(request.request_id, {"actual_model": route.model_id}),
        )
        valid = (
            attestation.request_id == request.request_id
            and attestation.requested_alias == request.requested_alias
            and attestation.resolved_route == route
            and attestation.actual_route == route
        )
        checks.append(ConformanceCheck("attestation", valid, "valid" if valid else "mismatch"))
    except Exception as exc:
        checks.append(ConformanceCheck("attestation", False, f"failed:{type(exc).__name__}"))


def _check_streaming(
    adapter: GatewayAdapter,
    manifest: AdapterManifest,
    request: AdapterRequest,
    checks: list[ConformanceCheck],
) -> None:
    status = manifest.capability_status(AdapterCapability.STREAMING)
    try:
        if status is not CapabilitySupport.SUPPORTED:
            checks.append(_optional("streaming", status))
            return
        translated = adapter.translate(
            AdapterRequest(
                request.request_id, request.protocol, request.requested_alias, request.payload, True
            )
        )
        valid = translated.stream and translated.request_id == request.request_id
        checks.append(ConformanceCheck("streaming", valid, "valid" if valid else "not_preserved"))
    except Exception as exc:
        checks.append(ConformanceCheck("streaming", False, f"failed:{type(exc).__name__}"))


def _check_cancellation(
    adapter: GatewayAdapter,
    manifest: AdapterManifest,
    request: AdapterRequest,
    checks: list[ConformanceCheck],
) -> None:
    status = manifest.capability_status(AdapterCapability.CANCELLATION)
    try:
        if status is not CapabilitySupport.SUPPORTED:
            checks.append(_optional("cancellation", status))
            return
        cancelled = adapter.cancel(request.request_id)
        checks.append(
            ConformanceCheck(
                "cancellation", cancelled is True, "accepted" if cancelled else "rejected"
            )
        )
    except Exception as exc:
        checks.append(ConformanceCheck("cancellation", False, f"failed:{type(exc).__name__}"))


def _check_failures(
    adapter: GatewayAdapter, manifest: AdapterManifest, checks: list[ConformanceCheck]
) -> None:
    try:
        if not _supported(manifest, AdapterCapability.FAILURE_NORMALIZATION):
            raise GatewayAdapterError("capability unavailable")
        failure = adapter.normalize_failure(
            AdapterFailureSignal("fixture_timeout", 504, timed_out=True)
        )
        valid = (
            failure.failure_class is NormalizedFailureClass.TIMEOUT
            and failure.retryable
            and failure.status_code == 504
        )
        checks.append(
            ConformanceCheck("failure_normalization", valid, "valid" if valid else "mismatch")
        )
    except Exception as exc:
        checks.append(
            ConformanceCheck("failure_normalization", False, f"failed:{type(exc).__name__}")
        )


def _check_telemetry(
    adapter: GatewayAdapter,
    manifest: AdapterManifest,
    request: AdapterRequest,
    checks: list[ConformanceCheck],
) -> None:
    status = manifest.capability_status(AdapterCapability.TELEMETRY)
    try:
        if status is not CapabilitySupport.SUPPORTED:
            checks.append(_optional("telemetry", status))
            return
        telemetry = validate_telemetry(manifest, adapter.telemetry(request.request_id))
        valid = telemetry.get("request_id") == request.request_id
        checks.append(ConformanceCheck("telemetry", valid, "valid" if valid else "mismatch"))
    except Exception as exc:
        checks.append(ConformanceCheck("telemetry", False, f"failed:{type(exc).__name__}"))


__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "run_conformance",
    "run_gateway_adapter_conformance",
]
