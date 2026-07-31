"""Runtime response, failure, and telemetry contracts for gateway adapters."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from verdict.gateway_adapters import (
    TELEMETRY_FIELDS,
    AdapterManifest,
    AdapterRequest,
    AdapterRouteIdentity,
    GatewayAdapterError,
    NormalizedFailureClass,
    TranslatedRequest,
    _json_mapping,
    _non_empty,
    _reject_secrets,
    _status_code,
    _strict,
)

TelemetryValue = None | bool | int | float | str


@dataclass(frozen=True)
class AdapterResponseMetadata:
    request_id: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        _non_empty(self.request_id, "response.request_id")
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "response.metadata"))


@dataclass(frozen=True)
class RouteIdentityAttestation:
    request_id: str
    requested_alias: str
    resolved_route: AdapterRouteIdentity
    actual_route: AdapterRouteIdentity | None
    source: str

    def __post_init__(self) -> None:
        for name in ("request_id", "requested_alias", "source"):
            _non_empty(getattr(self, name), f"attestation.{name}")
        if not isinstance(self.resolved_route, AdapterRouteIdentity) or (
            self.actual_route is not None
            and not isinstance(self.actual_route, AdapterRouteIdentity)
        ):
            raise GatewayAdapterError("attestation routes must be route identities")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RouteIdentityAttestation:
        payload = _strict(
            value,
            {"request_id", "requested_alias", "resolved_route", "actual_route", "source"},
            "attestation",
        )
        actual = payload["actual_route"]
        return cls(
            payload["request_id"],
            payload["requested_alias"],
            AdapterRouteIdentity.from_dict(payload["resolved_route"]),
            None if actual is None else AdapterRouteIdentity.from_dict(actual),
            payload["source"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "requested_alias": self.requested_alias,
            "resolved_route": self.resolved_route.to_dict(),
            "actual_route": None if self.actual_route is None else self.actual_route.to_dict(),
            "source": self.source,
        }


@dataclass(frozen=True)
class AdapterFailureSignal:
    code: str
    status_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        _non_empty(self.code, "failure.code")
        _status_code(self.status_code)
        if not isinstance(self.timed_out, bool) or not isinstance(self.cancelled, bool):
            raise GatewayAdapterError("failure flags must be boolean")


@dataclass(frozen=True)
class NormalizedFailure:
    failure_class: NormalizedFailureClass
    retryable: bool
    status_code: int | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "failure_class", NormalizedFailureClass(self.failure_class))
        except ValueError as exc:
            raise GatewayAdapterError("failure_class is unknown") from exc
        if not isinstance(self.retryable, bool):
            raise GatewayAdapterError("retryable must be boolean")
        _status_code(self.status_code)


@runtime_checkable
class GatewayAdapter(Protocol):
    @property
    def manifest(self) -> AdapterManifest: ...

    def discover(self) -> Sequence[AdapterRouteIdentity]: ...

    def translate(self, request: AdapterRequest) -> TranslatedRequest: ...

    def attest(
        self, request: TranslatedRequest, response: AdapterResponseMetadata
    ) -> RouteIdentityAttestation: ...

    def cancel(self, request_id: str) -> bool: ...

    def normalize_failure(self, signal: AdapterFailureSignal) -> NormalizedFailure: ...

    def telemetry(self, request_id: str) -> Mapping[str, TelemetryValue]: ...


def validate_telemetry(
    manifest: AdapterManifest, values: Mapping[str, TelemetryValue]
) -> dict[str, TelemetryValue]:
    if not isinstance(values, Mapping):
        raise GatewayAdapterError("telemetry must be an object")
    _reject_secrets(values, "telemetry")
    unknown = set(values) - set(manifest.telemetry_allowlist)
    if unknown:
        raise GatewayAdapterError(f"telemetry field(s) not allowed: {sorted(unknown)}")
    result: dict[str, TelemetryValue] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool, type(None))):
            raise GatewayAdapterError("telemetry values must be JSON scalars")
        if isinstance(value, float) and not math.isfinite(value):
            raise GatewayAdapterError("telemetry values must be finite")
        result[key] = value
    return {key: result[key] for key in sorted(result)}


__all__ = [
    "TELEMETRY_FIELDS",
    "AdapterFailureSignal",
    "AdapterResponseMetadata",
    "GatewayAdapter",
    "NormalizedFailure",
    "RouteIdentityAttestation",
    "TelemetryValue",
    "validate_telemetry",
]
