"""Protocol-based OmniRoute runtime availability and eligibility adapter.

The adapter consumes documented, JSON-like catalog/runtime observations through an
injected transport.  It deliberately has no knowledge of OmniRoute's private
storage or credentials; API, CLI, MCP, and A2A clients can implement the small
transport protocol below.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from llm_gate.models import ModelInfo


class AvailabilityState(str, Enum):
    ELIGIBLE = "eligible"
    READY = "eligible"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
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


class OmniRouteTransport(Protocol):
    """Documented transport seam; implementations may use API, CLI, MCP, or A2A."""

    def catalog(self) -> Any: ...

    def runtime(self) -> Any: ...


class StaticOmniRouteTransport:
    """Small fake-friendly transport useful for callers and tests."""

    def __init__(self, catalog: Any, runtime: Any = None) -> None:
        self._catalog = catalog
        self._runtime = runtime if runtime is not None else {}

    def catalog(self) -> Any:
        return self._catalog

    def runtime(self) -> Any:
        return self._runtime


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
        return None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None


def _capabilities(row: Mapping[str, Any]) -> frozenset[str]:
    value = row.get("capabilities", row.get("features", ()))
    if isinstance(value, Mapping):
        result = {str(k).lower() for k, v in value.items() if v is True}
        aliases = {
            "function_calling": "tools",
            "tool_calling": "tools",
            "json": "structured_output",
        }
        result.update(aliases[k] for k in tuple(result) if k in aliases)
        return frozenset(result)
    if isinstance(value, str):
        return frozenset(x.strip().lower() for x in value.split(",") if x.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(x).lower() for x in value)
    return frozenset()


def normalize_catalog(rows: Any) -> list[ModelInfo]:
    """Normalize common OmniRoute/OpenAI catalog envelopes without trusting them live."""
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
            row.get("provider") or (model_id.split("/", 1)[0] if "/" in model_id else "unknown")
        )
        tier = row.get("capability_tier", row.get("tier", 2))
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            tier = 2
        context = row.get("context_window", row.get("context", -1))
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
                capabilities=_capabilities(row),
                is_available=False,
                availability_state=AvailabilityState.UNKNOWN.value,
                source="catalog",
            )
        )
    return result


def _raw_observation(value: Any) -> RuntimeObservation:
    if isinstance(value, RuntimeObservation):
        return value
    if not isinstance(value, Mapping):
        return RuntimeObservation(error="malformed runtime observation", health="unknown")
    return RuntimeObservation(
        observed_at=value.get("observed_at", value.get("timestamp")),
        ttl_seconds=int(value.get("ttl_seconds", value.get("ttl", 60)) or 60),
        source=str(value.get("source", "unknown")),
        health=str(value.get("health", value.get("status", "unknown"))).lower(),
        quota_remaining_pct=_as_float(
            value.get("quota_remaining_pct", value.get("quota_remaining"))
        ),
        headroom_pct=_as_float(value.get("headroom_pct", value.get("headroom"))),
        budget_remaining=_as_float(value.get("budget_remaining")),
        cost=_as_float(value.get("cost")),
        concurrency=value.get("concurrency"),
        max_concurrency=value.get("max_concurrency"),
        auth=str(value.get("auth", "unknown")).lower(),
        circuit=str(value.get("circuit", "closed")).lower(),
        cooldown_until=value.get("cooldown_until"),
        lockout_until=value.get("lockout_until"),
        eligible=value.get("eligible"),
        error=value.get("error"),
        raw=value,
    )


def normalize_observation(
    model: ModelInfo, observation: RuntimeObservation, *, now: datetime | None = None
) -> AvailabilityCandidate:
    """Apply conservative precedence to contradictory runtime signals."""
    current = _now(now)
    obs = _raw_observation(observation)
    seen = _timestamp(obs.observed_at)
    age = (current - seen).total_seconds() if seen else None
    reasons: list[str] = []
    if obs.error or (obs.observed_at is not None and seen is None):
        return AvailabilityCandidate(
            model,
            AvailabilityState.MALFORMED,
            (obs.error or "malformed timestamp",),
            obs.headroom_pct,
            obs.source,
            age,
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
    quota = obs.quota_remaining_pct if obs.quota_remaining_pct is not None else obs.headroom_pct
    if quota is not None and (quota < 0 or quota > 100):
        return AvailabilityCandidate(
            model, AvailabilityState.MALFORMED, ("quota outside 0..100",), quota, obs.source, age
        )
    if quota is not None and quota <= 0:
        return AvailabilityCandidate(
            model, AvailabilityState.QUOTA_EXHAUSTED, ("quota exhausted",), quota, obs.source, age
        )
    contradictory = (obs.health in {"healthy", "ready", "ok"} and obs.eligible is False) or (
        obs.health in {"unhealthy", "down", "offline"} and obs.eligible is True
    )
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
    missing = sorted(requirements.required - model.capabilities)
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
        reason = _policy_reason(item, requirements)
        if reason:
            continue
        if item.state in {AvailabilityState.READY, AvailabilityState.DEGRADED} or (
            item.state is AvailabilityState.UNKNOWN
            and requirements.unknown_is_eligible
            and not requirements.protected
            and "stale observation" not in item.reasons
        ):
            result.append(item)
    return sorted(result, key=lambda x: (x.model.capability_tier, x.model.id))


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
        elif item.state in {AvailabilityState.READY, AvailabilityState.DEGRADED} or (
            item.state is AvailabilityState.UNKNOWN
            and requirements.unknown_is_eligible
            and not requirements.protected
        ):
            rows.append(
                {
                    "model": item.model.id,
                    "state": "eligible",
                    "rejected": False,
                    "reason": "eligible",
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


class OmniRouteAvailabilityAdapter:
    """Fetch and normalize runtime truth using an injected documented transport."""

    def __init__(
        self, transport: OmniRouteTransport, *, ttl_seconds: int = 60, clock: Any = None
    ) -> None:
        self.transport = transport
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def evaluate(
        self,
        requirements: CandidateRequirements = CandidateRequirements(),
        *,
        now: datetime | None = None,
    ) -> AvailabilityReport:
        current = _now(now or (self.clock() if self.clock else None))
        errors: list[str] = []
        try:
            catalog = normalize_catalog(self.transport.catalog())
        except (TimeoutError, OSError) as exc:
            return AvailabilityReport(
                (), (), "omniroute", None, (f"catalog transport: {type(exc).__name__}",)
            )
        except Exception:
            return AvailabilityReport((), (), "omniroute", None, ("catalog transport: malformed",))
        try:
            runtime = self.transport.runtime()
        except TimeoutError:
            runtime, errors = {}, ["runtime transport: timeout"]
        except Exception:
            runtime, errors = {}, ["runtime transport: malformed"]
        mapping = runtime if isinstance(runtime, Mapping) else {}
        malformed_runtime = runtime is not None and not isinstance(runtime, Mapping)
        timed_out = any("timeout" in error for error in errors)
        states = []
        for model in catalog:
            if timed_out:
                states.append(
                    AvailabilityCandidate(
                        model,
                        AvailabilityState.TIMEOUT,
                        ("runtime transport timeout",),
                        None,
                        "omniroute",
                        None,
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
            value = mapping.get(model.id, mapping.get(model.provider, mapping.get("default", {})))
            states.append(normalize_observation(model, _raw_observation(value), now=current))
        # Budget and concurrency are hard runtime gates, not ranking hints.
        for index, item in enumerate(states):
            raw = _raw_observation(
                mapping.get(
                    item.model.id, mapping.get(item.model.provider, mapping.get("default", {}))
                )
            )
            reason = None
            if (
                requirements.budget_remaining is not None
                and raw.cost is not None
                and raw.cost > requirements.budget_remaining
            ):
                reason = "budget exceeded"
            if (
                requirements.max_concurrency is not None
                and raw.concurrency is not None
                and raw.concurrency >= requirements.max_concurrency
            ):
                reason = "concurrency limit reached"
            if reason:
                states[index] = replace(
                    item, state=AvailabilityState.POLICY_DENIED, reasons=(reason,)
                )
        eligible = select_capable_candidates(states, requirements)
        freshness = min(
            (x.freshness_seconds for x in states if x.freshness_seconds is not None), default=None
        )
        source = next((x.source for x in states if x.source != "unknown"), "omniroute")
        return AvailabilityReport(tuple(states), tuple(eligible), source, freshness, tuple(errors))

    check = evaluate


def adapter_from_transport(
    transport: OmniRouteTransport, **kwargs: Any
) -> OmniRouteAvailabilityAdapter:
    """Compatibility factory for API/CLI/MCP/A2A integrations."""
    return OmniRouteAvailabilityAdapter(transport, **kwargs)
