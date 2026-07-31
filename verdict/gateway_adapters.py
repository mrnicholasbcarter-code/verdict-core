"""Provider-neutral, secret-free gateway adapter declarations and requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeAlias

GATEWAY_ADAPTER_CONTRACT_VERSION = "1"
_NAME = re.compile(r"^[a-z][a-z0-9_.:/-]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_NAMES = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "headers",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

TelemetryValue: TypeAlias = None | bool | int | float | str


class GatewayAdapterError(ValueError):
    """Raised when adapter data is malformed, ambiguous, or secret-bearing."""


class AdapterCapability(str, Enum):
    DISCOVERY = "discovery"
    REQUEST_TRANSLATION = "request_translation"
    ROUTE_ATTESTATION = "route_attestation"
    STREAMING = "streaming"
    CANCELLATION = "cancellation"
    FAILURE_NORMALIZATION = "failure_normalization"
    TELEMETRY = "telemetry"


class CapabilitySupport(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class NormalizedFailureClass(str, Enum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    INVALID_REQUEST = "invalid_request"
    CAPABILITY = "capability"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    UPSTREAM = "upstream"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TELEMETRY_FIELDS = frozenset(
    {
        "cancelled",
        "failure_class",
        "input_tokens",
        "latency_ms",
        "output_tokens",
        "request_id",
        "route_key",
        "status_code",
        "stream_started",
    }
)


@dataclass(frozen=True)
class AdapterDiscoveryMetadata:
    """Metadata for locating an adapter factory; it does not configure it."""

    distribution: str
    entrypoint: str
    implementation_digest: str

    def __post_init__(self) -> None:
        _non_empty(self.distribution, "discovery.distribution")
        _non_empty(self.entrypoint, "discovery.entrypoint")
        _digest(self.implementation_digest, "discovery.implementation_digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdapterDiscoveryMetadata:
        return cls(
            **_strict(value, {"distribution", "entrypoint", "implementation_digest"}, "discovery")
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "distribution": self.distribution,
            "entrypoint": self.entrypoint,
            "implementation_digest": self.implementation_digest,
        }


@dataclass(frozen=True)
class CapabilityNegotiation:
    required: tuple[str, ...]
    supported: tuple[str, ...]
    unavailable: tuple[str, ...]

    @property
    def admitted(self) -> bool:
        return not self.unavailable

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": list(self.required),
            "supported": list(self.supported),
            "unavailable": list(self.unavailable),
            "admitted": self.admitted,
        }


@dataclass(frozen=True)
class AdapterManifest:
    """Versioned adapter declaration with no credentials or gateway config."""

    adapter_id: str
    adapter_version: str
    protocol: str
    protocol_version: str
    capabilities: Mapping[AdapterCapability, CapabilitySupport]
    discovery: AdapterDiscoveryMetadata
    telemetry_allowlist: tuple[str, ...] = ()
    contract_version: str = GATEWAY_ADAPTER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _version(self.contract_version)
        for name in ("adapter_id", "protocol", "adapter_version", "protocol_version"):
            value = _non_empty(getattr(self, name), name)
            if name in {"adapter_id", "protocol"} and _NAME.fullmatch(value) is None:
                raise GatewayAdapterError(f"{name} has invalid characters")
        if not isinstance(self.discovery, AdapterDiscoveryMetadata):
            raise GatewayAdapterError("discovery must be adapter discovery metadata")
        if not isinstance(self.capabilities, Mapping):
            raise GatewayAdapterError("capabilities must be an object")
        normalized: dict[AdapterCapability, CapabilitySupport] = {}
        for capability, status in self.capabilities.items():
            try:
                normalized[AdapterCapability(capability)] = CapabilitySupport(status)
            except ValueError as exc:
                raise GatewayAdapterError("capabilities contain an unknown value") from exc
        object.__setattr__(self, "capabilities", MappingProxyType(normalized))
        fields = _string_tuple(self.telemetry_allowlist, "telemetry_allowlist")
        unknown = set(fields) - TELEMETRY_FIELDS
        if unknown:
            raise GatewayAdapterError(f"telemetry field(s) unknown: {sorted(unknown)}")
        object.__setattr__(self, "telemetry_allowlist", tuple(sorted(set(fields))))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdapterManifest:
        payload = _strict(
            value,
            {
                "contract_version",
                "adapter_id",
                "adapter_version",
                "protocol",
                "protocol_version",
                "capabilities",
                "discovery",
                "telemetry_allowlist",
            },
            "manifest",
        )
        capabilities = payload["capabilities"]
        if not isinstance(capabilities, Mapping):
            raise GatewayAdapterError("manifest.capabilities must be an object")
        return cls(
            contract_version=payload["contract_version"],
            adapter_id=payload["adapter_id"],
            adapter_version=payload["adapter_version"],
            protocol=payload["protocol"],
            protocol_version=payload["protocol_version"],
            capabilities=dict(capabilities),
            discovery=AdapterDiscoveryMetadata.from_dict(payload["discovery"]),
            telemetry_allowlist=tuple(payload["telemetry_allowlist"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "capabilities": {
                capability.value: status.value for capability, status in self.capabilities.items()
            },
            "discovery": self.discovery.to_dict(),
            "telemetry_allowlist": list(self.telemetry_allowlist),
        }

    def capability_status(self, capability: AdapterCapability | str) -> CapabilitySupport:
        try:
            return self.capabilities.get(AdapterCapability(capability), CapabilitySupport.UNKNOWN)
        except ValueError:
            return CapabilitySupport.UNKNOWN

    def negotiate(self, required: Iterable[AdapterCapability | str]) -> CapabilityNegotiation:
        names = tuple(
            sorted(
                {item.value if isinstance(item, AdapterCapability) else item for item in required}
            )
        )
        supported = tuple(
            name for name in names if self.capability_status(name) is CapabilitySupport.SUPPORTED
        )
        return CapabilityNegotiation(
            names, supported, tuple(name for name in names if name not in supported)
        )

    @property
    def digest(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True)
class AdapterRouteIdentity:
    """Secret-free identity for a resolved or actually served route."""

    gateway_id: str
    route_id: str
    provider: str
    model_id: str
    protocol: str

    def __post_init__(self) -> None:
        for name in ("gateway_id", "route_id", "provider", "model_id", "protocol"):
            _non_empty(getattr(self, name), f"route.{name}")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdapterRouteIdentity:
        return cls(
            **_strict(
                value, {"gateway_id", "route_id", "provider", "model_id", "protocol"}, "route"
            )
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "gateway_id": self.gateway_id,
            "route_id": self.route_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "protocol": self.protocol,
        }

    @property
    def key(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True)
class AdapterRequest:
    request_id: str
    protocol: str
    requested_alias: str
    payload: Mapping[str, Any]
    stream: bool = False
    contract_version: str = GATEWAY_ADAPTER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _version(self.contract_version)
        for name in ("request_id", "protocol", "requested_alias"):
            _non_empty(getattr(self, name), name)
        if not isinstance(self.stream, bool):
            raise GatewayAdapterError("stream must be boolean")
        object.__setattr__(self, "payload", _json_mapping(self.payload, "payload"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdapterRequest:
        return cls(
            **_strict(
                value,
                {
                    "contract_version",
                    "request_id",
                    "protocol",
                    "requested_alias",
                    "payload",
                    "stream",
                },
                "request",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "protocol": self.protocol,
            "requested_alias": self.requested_alias,
            "payload": _thaw(self.payload),
            "stream": self.stream,
        }


@dataclass(frozen=True)
class TranslatedRequest:
    request_id: str
    protocol: str
    requested_alias: str
    payload: Mapping[str, Any]
    stream: bool
    operation: str = "invoke"
    contract_version: str = GATEWAY_ADAPTER_CONTRACT_VERSION

    def __post_init__(self) -> None:
        request = AdapterRequest(
            self.request_id,
            self.protocol,
            self.requested_alias,
            self.payload,
            self.stream,
            self.contract_version,
        )
        _non_empty(self.operation, "operation")
        object.__setattr__(self, "payload", request.payload)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TranslatedRequest:
        return cls(
            **_strict(
                value,
                {
                    "contract_version",
                    "request_id",
                    "protocol",
                    "requested_alias",
                    "payload",
                    "stream",
                    "operation",
                },
                "translated_request",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "protocol": self.protocol,
            "requested_alias": self.requested_alias,
            "payload": _thaw(self.payload),
            "stream": self.stream,
            "operation": self.operation,
        }


def _strict(value: Mapping[str, Any], required: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GatewayAdapterError(f"{name} must be an object")
    _reject_secrets(value, name)
    unknown = set(value) - required
    missing = required - set(value)
    if missing:
        raise GatewayAdapterError(f"{name} missing field(s): {sorted(missing)}")
    if unknown:
        raise GatewayAdapterError(f"{name} has unknown field(s): {sorted(unknown)}")
    return dict(value)


def _reject_secrets(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise GatewayAdapterError(f"{path} keys must be strings")
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_NAMES or normalized.endswith(
                ("_api_key", "_password", "_secret", "_token")
            ):
                raise GatewayAdapterError(f"secret-bearing field rejected: {path}.{key}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _json_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GatewayAdapterError(f"{name} must be an object")
    _reject_secrets(value, name)
    try:
        decoded: Any = json.loads(
            json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        frozen = _freeze(decoded)
        if not isinstance(frozen, Mapping):
            raise GatewayAdapterError(f"{name} must be an object")
        return frozen
    except (TypeError, ValueError) as exc:
        raise GatewayAdapterError(f"{name} must be JSON-compatible") from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return value


def _non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayAdapterError(f"{name} must be a non-empty string")
    return value


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise GatewayAdapterError(f"{name} must be an array of strings")
    return tuple(_non_empty(item, f"{name}[]") for item in value)


def _digest(value: Any, name: str) -> str:
    result = _non_empty(value, name)
    if _DIGEST.fullmatch(result) is None:
        raise GatewayAdapterError(f"{name} must be a lowercase sha256 digest")
    return result


def _version(value: Any) -> None:
    if value != GATEWAY_ADAPTER_CONTRACT_VERSION:
        raise GatewayAdapterError("contract_version must be '1'")


def _status_code(value: Any) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599
    ):
        raise GatewayAdapterError("status_code must be an HTTP status or null")


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


__all__ = [
    "GATEWAY_ADAPTER_CONTRACT_VERSION",
    "TELEMETRY_FIELDS",
    "AdapterCapability",
    "AdapterDiscoveryMetadata",
    "AdapterManifest",
    "AdapterRequest",
    "AdapterRouteIdentity",
    "CapabilityNegotiation",
    "CapabilitySupport",
    "GatewayAdapterError",
    "NormalizedFailureClass",
    "TranslatedRequest",
]
