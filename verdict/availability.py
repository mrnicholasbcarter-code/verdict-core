"""Protocol-based OmniRoute runtime availability and eligibility adapter.

The adapter consumes documented, JSON-like catalog/runtime observations through an
injected transport.  It deliberately has no knowledge of OmniRoute's private
storage or credentials; API, CLI, MCP, and A2A clients can implement the small
transport protocol below.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from verdict.models import ModelInfo


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


_CAPABILITY_ALIASES = {
    "function-calling": "tools",
    "function_calling": "tools",
    "tool-calling": "tools",
    "tool_calling": "tools",
    "json": "structured_output",
    "structured-output": "structured_output",
}


def canonical_capability(value: Any) -> str:
    """Return the shared capability vocabulary used by catalog and policy."""
    normalized = str(value).strip().lower()
    return _CAPABILITY_ALIASES.get(normalized, normalized)


class AvailabilityState(str, Enum):
    ELIGIBLE = "eligible"
    READY = "eligible"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    LOCKED_OUT = "locked_out"
    CIRCUIT_OPEN = "circuit_open"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    CAPABILITY_MISMATCH = "capability_mismatch"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True)
class CandidateRequirements:
    """Hard requirements applied before any ranking or cost preference."""

    required: frozenset[str] = frozenset()
    protected: bool = False
    allow_models: frozenset[str] = frozenset()
    deny_models: frozenset[str] = frozenset()
    allow_providers: frozenset[str] = frozenset()
    deny_providers: frozenset[str] = frozenset()
    budget_remaining: float | None = None
    max_concurrency: int | None = None
    unknown_is_eligible: bool = False
    allow_degraded: bool = False
    estimated_tokens: int | None = None
    estimated_cost: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required", frozenset(canonical_capability(value) for value in self.required)
        )
        budget = self.budget_remaining
        if budget is not None and (not _is_finite_number(budget) or budget < 0):
            raise ValueError("budget_remaining must be a finite non-negative number")
        if self.max_concurrency is not None and (
            type(self.max_concurrency) is not int or self.max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if self.estimated_tokens is not None and (
            type(self.estimated_tokens) is not int
            or not _is_finite_number(self.estimated_tokens)
            or self.estimated_tokens < 0
        ):
            raise ValueError("estimated_tokens must be a finite non-negative integer")
        if self.estimated_cost is not None and (
            not _is_finite_number(self.estimated_cost) or self.estimated_cost < 0
        ):
            raise ValueError("estimated_cost must be a finite non-negative number")
        if any(
            type(value) is not bool
            for value in (self.protected, self.unknown_is_eligible, self.allow_degraded)
        ):
            raise ValueError("candidate requirement flags must be booleans")


@dataclass(frozen=True)
class RuntimeObservation:
    """Normalized input accepted from an API/CLI/MCP/A2A boundary."""

    observed_at: datetime | str | None = None
    ttl_seconds: int = 60
    source: str = "unknown"
    health: str = "unknown"
    quota_remaining_pct: float | None = None
    headroom_pct: float | None = None
    budget_remaining: float | None = None
    cost: float | None = None
    concurrency: int | None = None
    max_concurrency: int | None = None
    auth: str = "unknown"
    circuit: str = "closed"
    cooldown_until: datetime | str | None = None
    lockout_until: datetime | str | None = None
    eligible: bool | None = None
    error: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    token_headroom: int | None = None


@dataclass(frozen=True)
class AvailabilityCandidate:
    model: ModelInfo
    state: AvailabilityState
    reasons: tuple[str, ...] = ()
    headroom_pct: float | None = None
    source: str = "unknown"
    freshness_seconds: float | None = None
    normalized: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AvailabilityReport:
    candidates: tuple[AvailabilityCandidate, ...]
    eligible: tuple[AvailabilityCandidate, ...]
    source: str
    freshness_seconds: float | None
    errors: tuple[str, ...] = ()


class OmniRouteTransportError(RuntimeError):
    """Typed documented transport failure for OmniRoute adapter boundaries."""

    def __init__(self, operation: str, detail: str) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation}: {detail}")


class OmniRouteTransportTimeout(OmniRouteTransportError):  # noqa: N818 - public API
    """Transport timed out while fetching a documented OmniRoute operation."""


class OmniRouteTransportMalformed(OmniRouteTransportError):  # noqa: N818 - public API
    """Transport returned malformed data for a documented OmniRoute operation."""


class OmniRouteTransportUnauthorized(OmniRouteTransportError):  # noqa: N818 - public API
    """Transport credentials were missing, invalid, or insufficient."""


class OmniRouteTransportUnsupported(OmniRouteTransportError):  # noqa: N818 - public API
    """Transport does not implement a documented OmniRoute operation."""


CATALOG_TRANSPORT_OPERATIONS = ("catalog", "list_models")
RUNTIME_TRANSPORT_OPERATIONS = ("runtime", "get_runtime")
CAPABILITY_TRANSPORT_OPERATIONS = ("discover_capabilities",)
SUPPORTED_TRANSPORT_OPERATIONS = (
    *CATALOG_TRANSPORT_OPERATIONS,
    *RUNTIME_TRANSPORT_OPERATIONS,
    *CAPABILITY_TRANSPORT_OPERATIONS,
)
_MISSING = object()


class OmniRouteTransport(Protocol):
    """Documented transport seam; implementations may use API, CLI, MCP, or A2A."""

    def catalog(self) -> Any: ...

    def runtime(self) -> Any: ...


class CallableOmniRouteTransport:
    """Adapter for API/CLI/MCP/A2A callables implementing documented operations."""

    def __init__(
        self,
        *,
        catalog: Callable[[], Any] | None = None,
        runtime: Callable[[], Any] | None = None,
        discover_capabilities: Callable[[], Any] | None = None,
    ) -> None:
        self._catalog = catalog
        self._runtime = runtime
        self._discover_capabilities = discover_capabilities
        self.configured_operations = frozenset(
            operation
            for operation, configured in (
                ("catalog", catalog is not None),
                ("runtime", runtime is not None),
                ("discover_capabilities", discover_capabilities is not None),
            )
            if configured
        )

    def catalog(self) -> Any:
        if self._catalog is None:
            raise OmniRouteTransportUnsupported("catalog", "expected catalog() or list_models()")
        return self._catalog()

    def runtime(self) -> Any:
        if self._runtime is None:
            raise OmniRouteTransportUnsupported("runtime", "expected runtime() or get_runtime()")
        return self._runtime()

    def discover_capabilities(self) -> Any:
        if self._discover_capabilities is None:
            raise OmniRouteTransportUnsupported(
                "discover_capabilities", "expected discover_capabilities()"
            )
        return self._discover_capabilities()


class MappingOmniRouteTransport:
    """Adapter exposing documented OmniRoute operations from a mapping payload."""

    def __init__(self, operations: Mapping[str, Any]) -> None:
        self._operations = dict(operations)
        unsupported = sorted(set(self._operations) - set(SUPPORTED_TRANSPORT_OPERATIONS))
        if unsupported:
            raise OmniRouteTransportUnsupported(
                unsupported[0],
                f"unsupported operation; expected one of {', '.join(SUPPORTED_TRANSPORT_OPERATIONS)}",
            )
        self.configured_operations = frozenset(
            operation
            for operation, names in (
                ("catalog", CATALOG_TRANSPORT_OPERATIONS),
                ("runtime", RUNTIME_TRANSPORT_OPERATIONS),
                ("discover_capabilities", CAPABILITY_TRANSPORT_OPERATIONS),
            )
            if any(name in self._operations for name in names)
        )

    def _resolve(self, *names: str) -> Any:
        for name in names:
            if name not in self._operations:
                continue
            value = self._operations[name]
            return value() if callable(value) else value
        raise OmniRouteTransportUnsupported(names[0], f"expected one of {', '.join(names)}")

    def catalog(self) -> Any:
        return self._resolve(*CATALOG_TRANSPORT_OPERATIONS)

    def runtime(self) -> Any:
        return self._resolve(*RUNTIME_TRANSPORT_OPERATIONS)

    def discover_capabilities(self) -> Any:
        return self._resolve(*CAPABILITY_TRANSPORT_OPERATIONS)


class StaticOmniRouteTransport:
    """Small fake-friendly transport useful for callers and tests."""

    def __init__(self, catalog: Any, runtime: Any = None, capabilities: Any = None) -> None:
        self._catalog = catalog
        self._runtime = runtime if runtime is not None else {}
        self._capabilities = capabilities
        self.configured_operations = frozenset({"catalog", "runtime"})

    def catalog(self) -> Any:
        return self._catalog

    def runtime(self) -> Any:
        return self._runtime

    def list_models(self) -> Any:
        return self.catalog()

    def get_runtime(self) -> Any:
        return self.runtime()

    def discover_capabilities(self) -> Any:
        if self._capabilities is not None:
            return self._capabilities
        return self._catalog


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _timestamp(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    try:
        parsed = None if value is None or isinstance(value, bool) else float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, str) and value.strip() != str(parsed):
        return None
    return parsed


def _capabilities(row: Mapping[str, Any]) -> frozenset[str]:
    value = row.get("capabilities", row.get("features", ()))
    if isinstance(value, Mapping):
        return frozenset(canonical_capability(k) for k, v in value.items() if v is True)
    if isinstance(value, str):
        return frozenset(canonical_capability(x) for x in value.split(",") if x.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(canonical_capability(x) for x in value)
    return frozenset()


def _capability_mapping(rows: Any) -> Mapping[str, frozenset[str]]:
    if isinstance(rows, Mapping):
        rows = rows.get("data", rows.get("models", rows.get("items", rows)))
        if isinstance(rows, Mapping):
            mapped: dict[str, frozenset[str]] = {}
            for key, value in rows.items():
                if not isinstance(key, str):
                    continue
                payload = value if isinstance(value, Mapping) else {"capabilities": value}
                mapped[key] = _capabilities(payload)
            return mapped
    if not isinstance(rows, list):
        return {}
    result: dict[str, frozenset[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        result[row["id"]] = _capabilities(row)
    return result


def normalize_catalog(rows: Any, capabilities: Any = None) -> list[ModelInfo]:
    """Normalize common OmniRoute/OpenAI catalog envelopes without trusting them live."""
    capability_map = _capability_mapping(capabilities)
    if isinstance(rows, Mapping):
        rows = rows.get("data", rows.get("models", rows.get("items", [])))
    if not isinstance(rows, list):
        return []
    result: list[ModelInfo] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        model_id = row["id"]
        provider = str(
            row.get("provider")
            or row.get("owned_by")
            or (model_id.split("/", 1)[0] if "/" in model_id else "unknown")
        )
        tier = row.get("capability_tier", row.get("tier", 2))
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            tier = 2
        context = row.get("context_window", row.get("context_length", row.get("context", -1)))
        try:
            context = int(context)
        except (TypeError, ValueError):
            context = -1
        result.append(
            ModelInfo(
                id=model_id,
                provider=provider,
                capability_tier=tier,
                context_window=context,
                capabilities=capability_map.get(model_id, _capabilities(row)),
                is_available=False,
                availability_state=AvailabilityState.UNKNOWN.value,
                source="catalog",
            )
        )
    return result


def _raw_observation(value: Any) -> RuntimeObservation:
    if isinstance(value, RuntimeObservation):
        return replace(
            value,
            health=value.health.lower() if isinstance(value.health, str) else value.health,
            auth=value.auth.lower() if isinstance(value.auth, str) else value.auth,
            circuit=value.circuit.lower() if isinstance(value.circuit, str) else value.circuit,
        )
    if not isinstance(value, Mapping):
        return RuntimeObservation(error="malformed runtime observation", health="unknown")
    observed_at = value.get("observed_at", value.get("timestamp"))
    ttl_value = value.get("ttl_seconds", value.get("ttl", 60))
    ttl_seconds = _as_int(ttl_value)
    health = value.get("health", value.get("status", "unknown"))
    source = value.get("source", "unknown")
    auth = value.get("auth", "unknown")
    circuit = value.get("circuit", "closed")
    cooldown_until = value.get("cooldown_until")
    lockout_until = value.get("lockout_until")
    eligible = value.get("eligible")
    error = value.get("error")
    nested_raw = value.get("raw")
    raw: Mapping[str, Any]
    if nested_raw is None:
        raw = value
    elif isinstance(nested_raw, Mapping):
        merged_raw = dict(value)
        merged_raw.pop("raw", None)
        merged_raw.update(nested_raw)
        raw = merged_raw
    else:
        raw = {}
    numeric_values = (
        value.get("quota_remaining_pct", value.get("quota_remaining")),
        value.get("headroom_pct", value.get("headroom")),
        value.get("budget_remaining"),
        value.get("cost"),
    )
    concurrency = value.get("concurrency")
    max_concurrency = value.get("max_concurrency")
    token_headroom_value = value.get("token_headroom")
    tokens_remaining_value = value.get("tokens_remaining")
    token_headroom = (
        token_headroom_value if token_headroom_value is not None else tokens_remaining_value
    )
    parsed_concurrency = _as_int(concurrency)
    parsed_max_concurrency = _as_int(max_concurrency)
    parsed_token_headroom = _as_int(token_headroom)
    conflicting_token_headroom = (
        token_headroom_value is not None
        and tokens_remaining_value is not None
        and _as_int(token_headroom_value) != _as_int(tokens_remaining_value)
    )
    malformed = (
        ttl_seconds is None
        or ttl_seconds <= 0
        or not isinstance(health, str)
        or not isinstance(source, str)
        or not isinstance(auth, str)
        or not isinstance(circuit, str)
        or (eligible is not None and not isinstance(eligible, bool))
        or (error is not None and not isinstance(error, str))
        or (nested_raw is not None and not isinstance(nested_raw, Mapping))
        or any(item is not None and _as_float(item) is None for item in numeric_values)
        or conflicting_token_headroom
        or (
            token_headroom is not None
            and (
                parsed_token_headroom is None
                or not _is_finite_number(parsed_token_headroom)
                or parsed_token_headroom < 0
            )
        )
        or (concurrency is not None and (parsed_concurrency is None or parsed_concurrency < 0))
        or (
            max_concurrency is not None
            and (parsed_max_concurrency is None or parsed_max_concurrency < 1)
        )
        or (cooldown_until is not None and not isinstance(cooldown_until, (datetime, str)))
        or (lockout_until is not None and not isinstance(lockout_until, (datetime, str)))
    )
    if malformed:
        return RuntimeObservation(
            observed_at=observed_at,
            source=source if isinstance(source, str) else "unknown",
            health="unknown",
            error="malformed runtime observation",
            raw=raw,
        )
    assert ttl_seconds is not None
    assert isinstance(source, str)
    assert isinstance(health, str)
    assert isinstance(auth, str)
    assert isinstance(circuit, str)
    return RuntimeObservation(
        observed_at=observed_at,
        ttl_seconds=ttl_seconds,
        source=source,
        health=health.lower(),
        quota_remaining_pct=_as_float(
            value.get("quota_remaining_pct", value.get("quota_remaining"))
        ),
        headroom_pct=_as_float(value.get("headroom_pct", value.get("headroom"))),
        token_headroom=parsed_token_headroom,
        budget_remaining=_as_float(value.get("budget_remaining")),
        cost=_as_float(value.get("cost")),
        concurrency=parsed_concurrency,
        max_concurrency=parsed_max_concurrency,
        auth=auth.lower(),
        circuit=circuit.lower(),
        cooldown_until=cooldown_until,
        lockout_until=lockout_until,
        eligible=eligible,
        error=error,
        raw=raw,
    )


_KNOWN_HEALTH_STATES = frozenset(
    {
        "",
        "unknown",
        "healthy",
        "ready",
        "ok",
        "degraded",
        "degraded_mode",
        "unhealthy",
        "down",
        "offline",
        "unavailable",
        "outage",
        "denied",
        "quota_exhausted",
        "rate_limited",
        "unauthorized",
        "locked_out",
        "circuit_open",
        "timeout",
        "malformed",
    }
)
_KNOWN_AUTH_STATES = frozenset(
    {"unknown", "authorized", "ok", "valid", "unauthorized", "forbidden", "invalid", "missing"}
)
_KNOWN_CIRCUIT_STATES = frozenset({"closed", "half_open", "open", "tripped"})


def _observation_is_well_formed(obs: RuntimeObservation) -> bool:
    numeric_values = (obs.quota_remaining_pct, obs.headroom_pct, obs.budget_remaining, obs.cost)
    return (
        type(obs.ttl_seconds) is int
        and obs.ttl_seconds > 0
        and isinstance(obs.source, str)
        and isinstance(obs.health, str)
        and obs.health in _KNOWN_HEALTH_STATES
        and isinstance(obs.auth, str)
        and obs.auth in _KNOWN_AUTH_STATES
        and isinstance(obs.circuit, str)
        and obs.circuit in _KNOWN_CIRCUIT_STATES
        and isinstance(obs.raw, Mapping)
        and (obs.eligible is None or isinstance(obs.eligible, bool))
        and (obs.error is None or isinstance(obs.error, str))
        and all(item is None or _is_finite_number(item) for item in numeric_values)
        and all(
            item is None or 0 <= item <= 100 for item in (obs.quota_remaining_pct, obs.headroom_pct)
        )
        and all(item is None or item >= 0 for item in (obs.budget_remaining, obs.cost))
        and (
            obs.token_headroom is None
            or (
                type(obs.token_headroom) is int
                and _is_finite_number(obs.token_headroom)
                and obs.token_headroom >= 0
            )
        )
        and (obs.concurrency is None or (type(obs.concurrency) is int and obs.concurrency >= 0))
        and (
            obs.max_concurrency is None
            or (type(obs.max_concurrency) is int and obs.max_concurrency >= 1)
        )
        and (
            obs.cooldown_until is None
            or (
                isinstance(obs.cooldown_until, (datetime, str))
                and _timestamp(obs.cooldown_until) is not None
            )
        )
        and (
            obs.lockout_until is None
            or (
                isinstance(obs.lockout_until, (datetime, str))
                and _timestamp(obs.lockout_until) is not None
            )
        )
    )


def _probe_state(obs: RuntimeObservation) -> tuple[AvailabilityState, str] | None:
    if obs.source != "verdict:probe":
        return None
    error_class = obs.raw.get("probe_error_class")
    status = obs.raw.get("probe_status")
    reported_state = obs.raw.get("probe_availability_state")
    detail = obs.raw.get("probe_error")
    usage_available = obs.raw.get("usage_available")
    http_status = obs.raw.get("http_status")
    known_statuses = {
        "ready",
        "usage_unavailable",
        "completion_unavailable",
        "failed",
        "timeout",
        "skipped",
    }
    if (
        not isinstance(status, str)
        or status not in known_statuses
        or (
            reported_state is not None
            and (
                not isinstance(reported_state, str)
                or reported_state not in {"ready", "degraded", "denied"}
            )
        )
        or (error_class is not None and not isinstance(error_class, str))
        or (detail is not None and not isinstance(detail, str))
        or (usage_available is not None and type(usage_available) is not bool)
        or (http_status is not None and type(http_status) is not int)
    ):
        return AvailabilityState.MALFORMED, "malformed probe metadata"
    error_states = {
        "unauthorized": AvailabilityState.UNAUTHORIZED,
        "quota_exhausted": AvailabilityState.QUOTA_EXHAUSTED,
        "rate_limited": AvailabilityState.RATE_LIMITED,
        "timeout": AvailabilityState.TIMEOUT,
        "malformed_response": AvailabilityState.MALFORMED,
        "upstream_error": AvailabilityState.UNAVAILABLE,
    }
    reason = (
        error_class
        if isinstance(error_class, str) and error_class in error_states
        else str(status or "probe")
    )
    if status in {"usage_unavailable", "completion_unavailable"}:
        expected_usage = status == "completion_unavailable"
        if (
            reported_state not in {None, "degraded"}
            or error_class is not None
            or type(http_status) is not int
            or not 200 <= http_status < 300
            or usage_available is not expected_usage
            or obs.eligible is not None
            or obs.health not in {"degraded", "degraded_mode"}
        ):
            return AvailabilityState.MALFORMED, "contradictory probe metadata"
        return AvailabilityState.DEGRADED, reason
    if status == "ready":
        if (
            reported_state not in {None, "ready"}
            or error_class is not None
            or usage_available is not True
            or type(http_status) is not int
            or not 200 <= http_status < 300
            or bool(detail)
            or obs.eligible is not True
            or obs.health not in {"healthy", "ready", "ok"}
            or obs.auth not in {"authorized", "ok", "valid"}
        ):
            return AvailabilityState.MALFORMED, "contradictory probe metadata"
        return None
    if obs.eligible is True:
        return AvailabilityState.MALFORMED, "contradictory probe metadata"
    if status == "timeout":
        if error_class != "timeout" or reported_state not in {None, "degraded", "denied"}:
            return AvailabilityState.MALFORMED, "contradictory probe metadata"
        if reported_state == "denied":
            return AvailabilityState.DENIED, "probe_denied"
        return AvailabilityState.TIMEOUT, reason
    if status == "failed":
        if not isinstance(error_class, str) or not error_class:
            return AvailabilityState.MALFORMED, "malformed probe metadata"
        if reported_state == "denied":
            return AvailabilityState.DENIED, "probe_denied"
        if error_class in error_states:
            return error_states[error_class], error_class
        return AvailabilityState.UNAVAILABLE, "probe_failed"
    if status == "skipped":
        if detail == "cooldown" and reported_state in {None, "degraded"}:
            return AvailabilityState.RATE_LIMITED, "cooldown"
        if detail == "quarantined" and reported_state in {None, "denied"}:
            return AvailabilityState.DENIED, "quarantined"
        return AvailabilityState.MALFORMED, "contradictory probe metadata"
    return AvailabilityState.MALFORMED, "malformed probe metadata"


def normalize_observation(
    model: ModelInfo, observation: RuntimeObservation, *, now: datetime | None = None
) -> AvailabilityCandidate:
    """Apply conservative precedence to contradictory runtime signals."""
    current = _now(now)
    obs = _raw_observation(observation)
    seen = _timestamp(obs.observed_at)
    age = (current - seen).total_seconds() if seen else None
    reasons: list[str] = []
    if (
        obs.error
        or not _observation_is_well_formed(obs)
        or (obs.observed_at is not None and seen is None)
    ):
        return AvailabilityCandidate(
            model,
            AvailabilityState.MALFORMED,
            (
                "malformed runtime observation"
                if obs.error is None or obs.error == "malformed runtime observation"
                else "runtime observation error",
            ),
            obs.headroom_pct,
            obs.source,
            age,
        )
    if seen is None:
        return AvailabilityCandidate(
            model,
            AvailabilityState.UNKNOWN,
            ("observation timestamp missing",),
            obs.headroom_pct,
            obs.source,
            None,
        )
    if seen is not None and age is not None and (age < -5 or age > max(0, obs.ttl_seconds)):
        return AvailabilityCandidate(
            model,
            AvailabilityState.UNKNOWN,
            ("stale observation",),
            obs.headroom_pct,
            obs.source,
            age,
        )
    cooldown = _timestamp(obs.cooldown_until)
    lockout = _timestamp(obs.lockout_until)
    if obs.auth in {"unauthorized", "forbidden", "invalid", "missing"}:
        return AvailabilityCandidate(
            model,
            AvailabilityState.UNAUTHORIZED,
            (f"auth: {obs.auth}",),
            obs.headroom_pct,
            obs.source,
            age,
        )
    if lockout and lockout > current:
        return AvailabilityCandidate(
            model,
            AvailabilityState.LOCKED_OUT,
            ("provider lockout active",),
            obs.headroom_pct,
            obs.source,
            age,
        )
    if obs.circuit in {"open", "tripped"}:
        return AvailabilityCandidate(
            model,
            AvailabilityState.CIRCUIT_OPEN,
            ("circuit open",),
            obs.headroom_pct,
            obs.source,
            age,
        )
    if cooldown and cooldown > current:
        return AvailabilityCandidate(
            model,
            AvailabilityState.RATE_LIMITED,
            ("cooldown active",),
            obs.headroom_pct,
            obs.source,
            age,
        )
    quota_values = [
        value for value in (obs.quota_remaining_pct, obs.headroom_pct) if value is not None
    ]
    quota = min(quota_values) if quota_values else None
    if quota is not None and (quota < 0 or quota > 100):
        return AvailabilityCandidate(
            model, AvailabilityState.MALFORMED, ("quota outside 0..100",), quota, obs.source, age
        )
    if quota is not None and quota <= 0:
        return AvailabilityCandidate(
            model, AvailabilityState.QUOTA_EXHAUSTED, ("quota exhausted",), quota, obs.source, age
        )

    def intrinsic_capacity_denial() -> AvailabilityCandidate | None:
        if (
            obs.cost is not None
            and obs.budget_remaining is not None
            and obs.cost > obs.budget_remaining
        ):
            return AvailabilityCandidate(
                model,
                AvailabilityState.POLICY_DENIED,
                ("budget headroom exceeded",),
                quota,
                obs.source,
                age,
            )
        if (
            obs.concurrency is not None
            and obs.max_concurrency is not None
            and obs.concurrency >= obs.max_concurrency
        ):
            return AvailabilityCandidate(
                model,
                AvailabilityState.POLICY_DENIED,
                ("concurrency limit reached",),
                quota,
                obs.source,
                age,
            )
        return None

    if obs.eligible is False:
        if obs.health in {"healthy", "ready", "ok"}:
            return AvailabilityCandidate(
                model,
                AvailabilityState.UNKNOWN,
                ("contradictory health and eligibility signals",),
                quota,
                obs.source,
                age,
            )
        return AvailabilityCandidate(
            model, AvailabilityState.DENIED, ("runtime marked ineligible",), quota, obs.source, age
        )
    probe_state = _probe_state(obs)
    if probe_state is not None:
        state, reason = probe_state
        if state is AvailabilityState.DEGRADED:
            capacity_denial = intrinsic_capacity_denial()
            if capacity_denial is not None:
                return capacity_denial
        return AvailabilityCandidate(model, state, (reason,), quota, obs.source, age)
    contradictory = obs.health in {"unhealthy", "down", "offline"} and obs.eligible is True
    if contradictory:
        return AvailabilityCandidate(
            model,
            AvailabilityState.UNKNOWN,
            ("contradictory health and eligibility signals",),
            quota,
            obs.source,
            age,
        )
    if obs.health in {"unhealthy", "down", "offline"}:
        return AvailabilityCandidate(
            model, AvailabilityState.DENIED, (f"health: {obs.health}",), quota, obs.source, age
        )
    explicit_health_states = {
        "unavailable": AvailabilityState.UNAVAILABLE,
        "outage": AvailabilityState.UNAVAILABLE,
        "denied": AvailabilityState.DENIED,
        "quota_exhausted": AvailabilityState.QUOTA_EXHAUSTED,
        "rate_limited": AvailabilityState.RATE_LIMITED,
        "unauthorized": AvailabilityState.UNAUTHORIZED,
        "locked_out": AvailabilityState.LOCKED_OUT,
        "circuit_open": AvailabilityState.CIRCUIT_OPEN,
        "timeout": AvailabilityState.TIMEOUT,
        "malformed": AvailabilityState.MALFORMED,
    }
    if obs.health in explicit_health_states:
        return AvailabilityCandidate(
            model,
            explicit_health_states[obs.health],
            (f"health: {obs.health}",),
            quota,
            obs.source,
            age,
        )
    capacity_denial = intrinsic_capacity_denial()
    if capacity_denial is not None:
        return capacity_denial
    if obs.health in {"degraded", "degraded_mode"} or (quota is not None and quota < 20):
        return AvailabilityCandidate(
            model,
            AvailabilityState.DEGRADED,
            ("low headroom" if quota is not None and quota < 20 else "health degraded",),
            quota,
            obs.source,
            age,
        )
    if obs.health in {"unknown", ""}:
        return AvailabilityCandidate(
            model, AvailabilityState.UNKNOWN, ("health unknown",), quota, obs.source, age
        )
    return AvailabilityCandidate(
        model, AvailabilityState.READY, tuple(reasons) or ("eligible",), quota, obs.source, age
    )


def _policy_reason(
    candidate: AvailabilityCandidate, requirements: CandidateRequirements
) -> str | None:
    model = candidate.model
    model_capabilities = frozenset(canonical_capability(value) for value in model.capabilities)
    missing = sorted(requirements.required - model_capabilities)
    if missing:
        return f"missing capability: {missing[0]}"
    if requirements.allow_models and model.id not in requirements.allow_models:
        return "model not in allowlist"
    if model.id in requirements.deny_models:
        return "model denied by policy"
    if requirements.allow_providers and model.provider not in requirements.allow_providers:
        return "provider not in allowlist"
    if model.provider in requirements.deny_providers:
        return "provider denied by policy"
    return None


def select_capable_candidates(
    states: list[AvailabilityCandidate], requirements: CandidateRequirements
) -> list[AvailabilityCandidate]:
    """Return only candidates that pass every hard availability and policy gate."""
    result: list[AvailabilityCandidate] = []
    for item in states:
        if _candidate_is_eligible(item, requirements):
            result.append(item)
    return sorted(result, key=lambda x: (x.model.capability_tier, x.model.id))


def _candidate_is_eligible(
    item: AvailabilityCandidate, requirements: CandidateRequirements
) -> bool:
    if _policy_reason(item, requirements):
        return False
    return _availability_state_is_eligible(item, requirements)


def _availability_state_is_eligible(
    item: AvailabilityCandidate, requirements: CandidateRequirements
) -> bool:
    if item.state is AvailabilityState.READY:
        return True
    if (
        item.state is AvailabilityState.DEGRADED
        and requirements.allow_degraded
        and not requirements.protected
    ):
        return True
    return (
        item.state is AvailabilityState.UNKNOWN
        and requirements.unknown_is_eligible
        and not requirements.protected
        and item.freshness_seconds is not None
        and item.reasons == ("health unknown",)
    )


def explain_candidates(
    states: list[AvailabilityCandidate], requirements: CandidateRequirements
) -> list[dict[str, Any]]:
    """Build deterministic, secret-free exclusion explanations."""
    rows = []
    for item in states:
        reason = _policy_reason(item, requirements)
        if reason:
            state, text = (
                AvailabilityState.CAPABILITY_MISMATCH
                if reason.startswith("missing capability")
                else AvailabilityState.POLICY_DENIED,
                reason,
            )
            rows.append(
                {"model": item.model.id, "state": state.value, "rejected": True, "reason": text}
            )
        elif _candidate_is_eligible(item, requirements):
            rows.append(
                {
                    "model": item.model.id,
                    "state": item.state.value,
                    "rejected": False,
                    "reason": item.reasons[0] if item.reasons else item.state.value,
                }
            )
        else:
            rows.append(
                {
                    "model": item.model.id,
                    "state": item.state.value,
                    "rejected": True,
                    "reason": item.reasons[0] if item.reasons else item.state.value,
                }
            )
    return sorted(rows, key=lambda x: x["model"])


def _call_transport_operation(
    transport: Any, names: tuple[str, ...], fallback: Any = _MISSING
) -> Any:
    last_error: OmniRouteTransportError | None = None
    for name in names:
        operation = getattr(transport, name, None)
        if operation is None:
            continue
        try:
            return operation() if callable(operation) else operation
        except OmniRouteTransportError as exc:
            last_error = exc
            break
        except TimeoutError as exc:
            raise OmniRouteTransportTimeout(name, type(exc).__name__) from exc
        except OSError as exc:
            raise OmniRouteTransportError(name, type(exc).__name__) from exc
        except Exception as exc:
            raise OmniRouteTransportMalformed(name, type(exc).__name__) from exc
    if fallback is not _MISSING:
        return fallback
    if last_error is not None:
        raise last_error
    raise OmniRouteTransportUnsupported(names[0], f"expected one of {', '.join(names)}")


def discover_transport_capabilities(transport: Any) -> frozenset[str]:
    configured = getattr(transport, "configured_operations", None)
    configured_limit: set[str] | None = None
    if isinstance(configured, (list, tuple, set, frozenset)):
        configured_limit = {
            str(operation)
            for operation in configured
            if str(operation) in {"catalog", "runtime", "discover_capabilities"}
        }
        operations = set(configured_limit)
    else:
        operations = set()
        for canonical, names in (
            ("catalog", CATALOG_TRANSPORT_OPERATIONS),
            ("runtime", RUNTIME_TRANSPORT_OPERATIONS),
        ):
            if any(hasattr(transport, name) for name in names):
                operations.add(canonical)
    explicit = getattr(transport, "discover_capabilities", None)
    if explicit is not None:
        try:
            payload = explicit() if callable(explicit) else explicit
        except OmniRouteTransportUnsupported:
            payload = None
        except OmniRouteTransportError:
            payload = None
        except TimeoutError:
            payload = None
        except Exception:
            payload = None
        if payload is not None:
            if isinstance(payload, Mapping):
                payload = payload.get("operations", payload.get("supported_operations", payload))
            if isinstance(payload, str):
                payload = [x.strip() for x in payload.split(",") if x.strip()]
            if isinstance(payload, (list, tuple, set, frozenset)):
                advertised = {str(item).strip().lower() for item in payload if str(item).strip()}
                allowlisted = advertised & set(SUPPORTED_TRANSPORT_OPERATIONS)
                if allowlisted:
                    discovered = {
                        "catalog"
                        if item in CATALOG_TRANSPORT_OPERATIONS
                        else "runtime"
                        if item in RUNTIME_TRANSPORT_OPERATIONS
                        else "discover_capabilities"
                        for item in allowlisted
                    }
                    operations.update(
                        discovered if configured_limit is None else discovered & configured_limit
                    )
    return frozenset(sorted(operations))


class OmniRouteAvailabilityAdapter:
    """Fetch and normalize runtime truth using an injected documented transport."""

    def __init__(
        self, transport: OmniRouteTransport, *, ttl_seconds: int = 60, clock: Any = None
    ) -> None:
        self.transport = transport
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.transport_capabilities = discover_transport_capabilities(transport)

    def evaluate(
        self,
        requirements: CandidateRequirements = CandidateRequirements(),
        *,
        now: datetime | None = None,
    ) -> AvailabilityReport:
        current = _now(now or (self.clock() if self.clock else None))
        errors: list[str] = []
        try:
            catalog_payload = _call_transport_operation(
                self.transport, CATALOG_TRANSPORT_OPERATIONS
            )
            capability_payload = _call_transport_operation(
                self.transport, CAPABILITY_TRANSPORT_OPERATIONS, fallback=None
            )
            catalog = normalize_catalog(catalog_payload, capability_payload)
        except OmniRouteTransportTimeout as exc:
            return AvailabilityReport(
                (), (), "omniroute", None, (f"{exc.operation} transport: timeout",)
            )
        except OmniRouteTransportUnauthorized as exc:
            return AvailabilityReport(
                (), (), "omniroute", None, (f"{exc.operation} transport: unauthorized",)
            )
        except OmniRouteTransportUnsupported as exc:
            return AvailabilityReport((), (), "omniroute", None, (str(exc),))
        except OmniRouteTransportMalformed as exc:
            return AvailabilityReport(
                (), (), "omniroute", None, (f"{exc.operation} transport: malformed",)
            )
        except OmniRouteTransportError as exc:
            return AvailabilityReport(
                (), (), "omniroute", None, (f"{exc.operation} transport: unavailable",)
            )
        runtime_failure_state: AvailabilityState | None = None
        try:
            runtime = _call_transport_operation(self.transport, RUNTIME_TRANSPORT_OPERATIONS)
        except OmniRouteTransportTimeout:
            runtime, errors = {}, ["runtime transport: timeout"]
            runtime_failure_state = AvailabilityState.TIMEOUT
        except OmniRouteTransportUnauthorized:
            runtime, errors = {}, ["runtime transport: unauthorized"]
            runtime_failure_state = AvailabilityState.UNAUTHORIZED
        except OmniRouteTransportUnsupported as exc:
            runtime, errors = {}, [str(exc)]
        except OmniRouteTransportMalformed:
            runtime, errors = {}, ["runtime transport: malformed"]
            runtime_failure_state = AvailabilityState.MALFORMED
        except OmniRouteTransportError:
            runtime, errors = {}, ["runtime transport: unavailable"]
            runtime_failure_state = AvailabilityState.UNAVAILABLE
        mapping = runtime if isinstance(runtime, Mapping) else {}
        malformed_runtime = runtime is not None and not isinstance(runtime, Mapping)

        def runtime_value(model: ModelInfo) -> object:
            keys = [model.id]
            if model.provider not in {"", "unknown"}:
                suffix = model.id.split("/", 1)[-1]
                canonical = f"{model.provider}/{suffix}"
                if canonical not in keys:
                    keys.append(canonical)
            keys.extend((model.provider, "default"))
            for key in keys:
                if key in mapping:
                    return mapping[key]
            return {}

        states = []
        for model in catalog:
            if runtime_failure_state is not None:
                states.append(
                    AvailabilityCandidate(
                        model, runtime_failure_state, (errors[0],), None, "omniroute", None
                    )
                )
                continue
            if malformed_runtime:
                states.append(
                    AvailabilityCandidate(
                        model,
                        AvailabilityState.MALFORMED,
                        ("runtime payload is not an object",),
                        None,
                        "omniroute",
                        None,
                    )
                )
                continue
            value = runtime_value(model)
            states.append(normalize_observation(model, _raw_observation(value), now=current))
        # Request capacity and concurrency are hard runtime gates, not ranking hints.
        for index, item in enumerate(states):
            if item.state is AvailabilityState.UNKNOWN:
                if not _availability_state_is_eligible(item, requirements):
                    continue
            elif item.state not in {AvailabilityState.READY, AvailabilityState.DEGRADED}:
                continue
            raw = _raw_observation(runtime_value(item.model))
            reasons = []
            known_costs = [
                value for value in (requirements.estimated_cost, raw.cost) if value is not None
            ]
            estimated_cost = max(known_costs) if known_costs else None
            budget_limits = [
                value
                for value in (requirements.budget_remaining, raw.budget_remaining)
                if value is not None
            ]
            if estimated_cost is not None and budget_limits:
                if estimated_cost > min(budget_limits):
                    reasons.append(
                        "budget headroom exceeded"
                        if requirements.estimated_cost is not None
                        or raw.budget_remaining is not None
                        else "budget exceeded"
                    )
            elif requirements.estimated_cost is not None:
                reasons.append("budget headroom unknown")
            if requirements.estimated_tokens is not None:
                if raw.token_headroom is None:
                    reasons.append("token headroom unknown")
                elif requirements.estimated_tokens > raw.token_headroom:
                    reasons.append("token headroom exceeded")
            if (
                requirements.max_concurrency is not None
                and raw.concurrency is not None
                and raw.concurrency >= requirements.max_concurrency
            ):
                reasons.append("concurrency limit reached")
            if reasons:
                denied = any(
                    reason.endswith("exceeded") or reason.endswith("reached") for reason in reasons
                )
                states[index] = replace(
                    item,
                    state=(
                        AvailabilityState.POLICY_DENIED if denied else AvailabilityState.DEGRADED
                    ),
                    reasons=tuple(reasons),
                )
        eligible = select_capable_candidates(states, requirements)
        freshness = max(
            (x.freshness_seconds for x in states if x.freshness_seconds is not None), default=None
        )
        source = next((x.source for x in states if x.source != "unknown"), "omniroute")
        return AvailabilityReport(tuple(states), tuple(eligible), source, freshness, tuple(errors))

    check = evaluate


class ProbeEnrichedAdapter:
    """Adapter that adds bounded live probes on top of catalog/runtime truth.

    This is the issue #57 root-cause wiring: the existing
    :class:`OmniRouteAvailabilityAdapter` only consults catalog + runtime
    metadata, never proving a model is actually usable.  This adapter reuses the
    already-built, already-tested :class:`~verdict.probes.ProbeRunner` and
    merges single-token probe observations into the same candidate states the
    router consumes.  No new transport code is introduced: the probe uses the
    documented ``openai_probe_transport`` (POST /v1/chat/completions), separate
    from the GET-only catalog/runtime transport.

    Probing is opt-in via ``enabled`` so the development/default profile stays
    pure catalog/runtime (per ENFORCEMENT_AND_LEARNING: probing is a
    production-profile concern).  When disabled the adapter is a thin pass-through
    to the wrapped adapter.
    """

    def __init__(
        self,
        base: OmniRouteAvailabilityAdapter,
        *,
        probe_transport: Any | None = None,
        enabled: bool = False,
        policy: Any | None = None,
        registry: Any | None = None,
        max_probed: int = 16,
        clock: Any = None,
    ) -> None:
        self.base = base
        self.probe_transport = probe_transport
        self.enabled = enabled
        # Lazy import: probes.py imports ``RuntimeObservation`` from this module
        # at load time, so importing it at the top would create a circular dep.
        self._runner = None
        if enabled:
            from verdict.probes import ProbeRunner

            self._runner = ProbeRunner(policy=policy, registry=registry)
        self.max_probed = max_probed
        self.clock = clock

    def evaluate(
        self,
        requirements: CandidateRequirements = CandidateRequirements(),
        *,
        now: datetime | None = None,
    ) -> AvailabilityReport:
        report = self.base.evaluate(requirements, now=now)
        if not self.enabled or self._runner is None or self.probe_transport is None:
            return report
        # Bound the probe fan-out to the most relevant candidates to keep the
        # one-token probe cheap and within ProbePolicy.max_models_per_run.
        model_ids = [c.model.id for c in report.candidates][: self.max_probed]
        if not model_ids:
            return report
        observations = self._runner.run(model_ids, self.probe_transport, now=now)
        probe_by_id: dict[str, RuntimeObservation] = {}
        for obs in observations:
            try:
                probe_by_id[obs.model_id] = obs.as_runtime_observation()
            except Exception:
                # A malformed probe result must never poison the report.
                continue
        if not probe_by_id:
            return report
        current = _now(now or (self.clock() if self.clock else None))
        merged = [
            self._merge_probe(candidate, probe_by_id, current) for candidate in report.candidates
        ]
        eligible = select_capable_candidates(merged, requirements)
        return AvailabilityReport(
            tuple(merged), tuple(eligible), report.source, report.freshness_seconds, report.errors
        )

    @staticmethod
    def _merge_probe(
        candidate: AvailabilityCandidate, probe_by_id: dict[str, RuntimeObservation], now: datetime
    ) -> AvailabilityCandidate:
        probe = probe_by_id.get(candidate.model.id)
        if probe is None:
            return candidate
        # Probe truth is authoritative when it contradicts catalog/runtime.
        probe_candidate = normalize_observation(candidate.model, probe, now=now)
        if probe_candidate.state is AvailabilityState.MALFORMED:
            # Keep the catalog/runtime verdict rather than degrading on a probe
            # metadata glitch.
            return candidate
        return probe_candidate

    check = evaluate


def adapter_from_transport(
    transport: OmniRouteTransport, **kwargs: Any
) -> OmniRouteAvailabilityAdapter:
    """Compatibility factory for API/CLI/MCP/A2A integrations."""
    return OmniRouteAvailabilityAdapter(transport, **kwargs)
