"""Versioned, privacy-safe runtime capability evidence.

Runtime ownership proves which process Verdict may manage; it does not prove
that the process implements a protocol or that a backend is usable.  This
module keeps those claims separate and maps only documented health responses
into capability observations.  Missing or ambiguous evidence remains
``unknown``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlsplit

RUNTIME_HEALTH_SCHEMA_VERSION = "1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T")


class RuntimeHealthError(ValueError):
    """Raised when a runtime-health artifact is malformed or unsafe."""


class RuntimeHealthStatus(str, Enum):
    """Evidence strength, deliberately distinct from model quality."""

    ABSENT = "absent"
    CONFIGURED = "configured"
    REACHABLE = "reachable"
    AUTHENTICATED = "authenticated"
    CATALOG_CLAIMED = "catalog_claimed"
    PROTOCOL_COMPATIBLE = "protocol_compatible"
    OBSERVED_WORKING = "observed_working"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class RuntimeHealthSource(Protocol):
    """Minimal runtime-plan boundary used to keep the health layer replaceable."""

    def to_dict(self) -> dict[str, Any]: ...


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise RuntimeHealthError("runtime-health data must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode()).hexdigest()}"


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeHealthError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: Any, name: str) -> str:
    result = _string(value, name)
    if _TIMESTAMP.match(result) is None:
        raise RuntimeHealthError(f"{name} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeHealthError(f"{name} must be an ISO-8601 timestamp") from exc
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeHealthError(f"{name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise RuntimeHealthError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise RuntimeHealthError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    return dict(value)


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise RuntimeHealthError(f"{name} must be an array of strings")
    return tuple(_string(item, f"{name}[]") for item in value)


def _endpoint_class(endpoint: str | None) -> str:
    if endpoint is None:
        return "none"
    parsed = urlsplit(_string(endpoint, "endpoint"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeHealthError("endpoint must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeHealthError("endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeHealthError("endpoint must not contain a query or fragment")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"127.0.0.1", "::1", "localhost", "ip6-localhost"}:
        network = "loopback"
    else:
        network = "named-host"
    return f"{network}-{parsed.scheme}"


@dataclass(frozen=True)
class RuntimeHealthObservation:
    """One bounded observation for one exact component instance."""

    component_id: str
    component_kind: str
    instance_id: str
    status: RuntimeHealthStatus
    observed_at: str
    source: str
    method: str
    endpoint: str | None = None
    endpoint_class: str = "none"
    version: str | None = None
    identity_verified: bool = False
    limitations: tuple[str, ...] = ()
    evidence_digest: str = ""
    schema_version: str = RUNTIME_HEALTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "component_id",
            "component_kind",
            "instance_id",
            "source",
            "method",
            "endpoint_class",
        ):
            _string(getattr(self, name), name)
        try:
            object.__setattr__(self, "status", RuntimeHealthStatus(self.status))
        except ValueError as exc:
            raise RuntimeHealthError("status is invalid") from exc
        _timestamp(self.observed_at, "observed_at")
        if self.endpoint is not None and _endpoint_class(self.endpoint) != self.endpoint_class:
            raise RuntimeHealthError("endpoint_class does not match endpoint")
        if self.endpoint is None and self.endpoint_class != "none":
            raise RuntimeHealthError("endpoint_class must be none when endpoint is absent")
        if self.version is not None:
            _string(self.version, "version")
        if not isinstance(self.identity_verified, bool):
            raise RuntimeHealthError("identity_verified must be boolean")
        object.__setattr__(self, "limitations", _strings(self.limitations, "limitations"))
        if self.schema_version != RUNTIME_HEALTH_SCHEMA_VERSION:
            raise RuntimeHealthError("unsupported runtime-health schema version")
        expected = _digest(self._payload())
        if self.evidence_digest:
            if _DIGEST.fullmatch(self.evidence_digest) is None:
                raise RuntimeHealthError("evidence_digest must be a sha256 digest")
            if self.evidence_digest != expected:
                raise RuntimeHealthError("evidence_digest does not match observation")
        else:
            object.__setattr__(self, "evidence_digest", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "component_id": self.component_id,
            "component_kind": self.component_kind,
            "instance_id": self.instance_id,
            "status": self.status.value,
            "observed_at": self.observed_at,
            "source": self.source,
            "method": self.method,
            "endpoint": self.endpoint,
            "endpoint_class": self.endpoint_class,
            "version": self.version,
            "identity_verified": self.identity_verified,
            "limitations": list(self.limitations),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "evidence_digest": self.evidence_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeHealthObservation:
        payload = _strict(
            value,
            required={
                "schema_version",
                "component_id",
                "component_kind",
                "instance_id",
                "status",
                "observed_at",
                "source",
                "method",
                "endpoint",
                "endpoint_class",
                "version",
                "identity_verified",
                "limitations",
                "evidence_digest",
            },
            optional=set(),
            name="runtime_health_observation",
        )
        return cls(**payload)


@dataclass(frozen=True)
class RuntimeHealthReport:
    """Machine-readable runtime capability explanation."""

    generated_at: str
    status: str
    observations: tuple[RuntimeHealthObservation, ...]
    summary: Mapping[str, int]
    errors: tuple[str, ...] = ()
    schema_version: str = RUNTIME_HEALTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.generated_at, "generated_at")
        if self.status not in {"ready", "degraded", "unknown", "blocked"}:
            raise RuntimeHealthError("report status is invalid")
        if self.schema_version != RUNTIME_HEALTH_SCHEMA_VERSION:
            raise RuntimeHealthError("unsupported runtime-health schema version")
        observations = tuple(self.observations)
        if any(not isinstance(item, RuntimeHealthObservation) for item in observations):
            raise RuntimeHealthError("observations must contain RuntimeHealthObservation values")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "summary", dict(self.summary))
        object.__setattr__(self, "errors", _strings(self.errors, "errors"))

    @property
    def passed(self) -> bool:
        """Report generation succeeded; unknown optional services are not failures."""
        return self.status != "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "status": self.status,
            "passed": self.passed,
            "summary": dict(self.summary),
            "observations": [item.to_dict() for item in self.observations],
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeHealthReport:
        payload = _strict(
            value,
            required={
                "schema_version",
                "generated_at",
                "status",
                "passed",
                "summary",
                "observations",
                "errors",
            },
            optional=set(),
            name="runtime_health_report",
        )
        if payload["passed"] != (payload["status"] != "blocked"):
            # ``passed`` is derived; reject contradictory artifacts without
            # depending on a caller-provided boolean.
            raise RuntimeHealthError("passed does not match report status")
        return cls(
            generated_at=payload["generated_at"],
            status=payload["status"],
            observations=tuple(
                RuntimeHealthObservation.from_dict(item) for item in payload["observations"]
            ),
            summary=payload["summary"],
            errors=tuple(payload["errors"]),
            schema_version=payload["schema_version"],
        )


def _service_observations(
    service: Mapping[str, Any], observed_at: str
) -> list[RuntimeHealthObservation]:
    component_id = _string(service.get("service_id"), "service.service_id")
    component_kind = _string(service.get("kind", "runtime"), "service.kind")
    endpoint = service.get("health_endpoint") or service.get("endpoint")
    endpoint = endpoint if isinstance(endpoint, str) else None
    endpoint_class = _endpoint_class(endpoint)
    owner_pid = service.get("owner_pid")
    instance_id = f"pid:{owner_pid}" if isinstance(owner_pid, int) else "unowned"
    observations = [
        RuntimeHealthObservation(
            component_id=component_id,
            component_kind=component_kind,
            instance_id=instance_id,
            status=RuntimeHealthStatus.CONFIGURED,
            observed_at=observed_at,
            source="verdict:runtime-contract",
            method="versioned-service-spec",
            endpoint=endpoint,
            endpoint_class=endpoint_class,
            identity_verified=isinstance(owner_pid, int),
            limitations=("configuration does not prove liveness",),
        )
    ]
    health = service.get("health")
    service_status = service.get("status")
    if health == "healthy":
        status = RuntimeHealthStatus.REACHABLE
        limitations = ("health endpoint does not prove protocol compatibility or task success",)
        method = "documented-health-http"
    elif health == "unhealthy":
        status = RuntimeHealthStatus.DEGRADED
        limitations = ("documented health endpoint returned a non-success status",)
        method = "documented-health-http"
    else:
        status = RuntimeHealthStatus.UNKNOWN
        method = "documented-health-http"
        if service_status == "ambiguous":
            limitations = ("process identity is ambiguous; no liveness claim made",)
        elif service.get("port_state") == "occupied" and owner_pid is None:
            limitations = (
                "port is occupied without a verified owner; not treated as service health",
            )
        elif endpoint is None:
            limitations = ("no documented health endpoint is available",)
        else:
            limitations = ("documented health endpoint was unavailable",)
    observations.append(
        RuntimeHealthObservation(
            component_id=component_id,
            component_kind=component_kind,
            instance_id=instance_id,
            status=status,
            observed_at=observed_at,
            source="verdict:runtime-health",
            method=method,
            endpoint=endpoint,
            endpoint_class=endpoint_class,
            identity_verified=isinstance(owner_pid, int),
            limitations=limitations,
        )
    )
    return observations


def build_runtime_health_report(
    plan: RuntimeHealthSource | Mapping[str, Any] | Any, *, observed_at: str | None = None
) -> RuntimeHealthReport:
    """Build a truthful report from a read-only runtime plan."""
    raw = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan)
    if "status" not in raw or "services" not in raw:
        raise RuntimeHealthError("runtime plan must include status and services")
    timestamp = _timestamp(observed_at or _now(), "observed_at")
    services = raw.get("services", ())
    if not isinstance(services, Sequence) or isinstance(services, (str, bytes)):
        raise RuntimeHealthError("runtime plan services must be an array")
    observations: list[RuntimeHealthObservation] = []
    for service in services:
        if not isinstance(service, Mapping):
            raise RuntimeHealthError("runtime plan service must be an object")
        observations.extend(_service_observations(service, timestamp))
    errors = tuple(str(item) for item in raw.get("errors", ()) if isinstance(item, str))
    counts = {status.value: 0 for status in RuntimeHealthStatus}
    for observation in observations:
        counts[observation.status.value] += 1
    dynamic = [item.status for item in observations if item.source == "verdict:runtime-health"]
    if errors:
        status = "blocked"
    elif any(item is RuntimeHealthStatus.DEGRADED for item in dynamic):
        status = "degraded"
    elif any(
        item in {RuntimeHealthStatus.REACHABLE, RuntimeHealthStatus.OBSERVED_WORKING}
        for item in dynamic
    ):
        status = "ready"
    else:
        status = "unknown"
    return RuntimeHealthReport(
        generated_at=timestamp,
        status=status,
        observations=tuple(observations),
        summary=counts,
        errors=errors,
    )


__all__ = [
    "RUNTIME_HEALTH_SCHEMA_VERSION",
    "RuntimeHealthError",
    "RuntimeHealthObservation",
    "RuntimeHealthReport",
    "RuntimeHealthSource",
    "RuntimeHealthStatus",
    "build_runtime_health_report",
]
