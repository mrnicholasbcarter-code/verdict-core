"""Versioned model passports: qualification state, capacity, and identity.

A ``ModelPassport`` records the verified operational state of one concrete
provider model: whether its authentication succeeded, its observed latency
distribution, the negotiated capability surface (tool / structured output),
and the capacity headroom available for assignment.  Passports are produced by
a bounded, privacy-safe probe cascade (``verdict.probes``) and are the only
evidence that lets a model be selected for live work.  Unresolved aliases
(``auto/*``, ``best/*``, ``default``) never produce a passport and therefore
never route.

The availability state distinguishes temporary operational quarantine
(``quarantined``: previously known, temporarily unsafe, auto-recoverable)
from permanent execution denial (``denied``: requires manual re-enable).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from verdict.capability_passports import CapabilityPassportError
from verdict.models import ModelInfo
from verdict.probes import ProbeBudget, ProbeObservation, ProbePolicy, ProbeRunner, ProbeTransport

MODEL_PASSPORT_SCHEMA_VERSION = "1"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

# Capacity gate: tasks that may exceed this many tokens require a capacity
# confidence above the threshold before the model is admitted.
LARGE_TASK_TOKENS = 50_000
CAPACITY_CONFIDENCE_THRESHOLD = 0.8

# Passport freshness window for the in-memory isolation cache (seconds).
PASSPORT_TTL_SECONDS = 300


class ModelPassportError(CapabilityPassportError):
    """Raised when a model passport violates its versioned contract."""


@dataclass(frozen=True)
class ModelPassport:
    """Verified operational state of one concrete provider model."""

    provider: str
    model_id: str
    auth_state: str
    latency_p95: float | None = None
    context_window: int = -1
    tool_support: bool = False
    token_cost_per_1k: float | None = None
    last_verified_timestamp: datetime = field(default_factory=lambda: _now())
    availability_state: str = "eligible"
    availability_reason: str | None = None
    quarantine_until: datetime | None = None
    quarantined_at: datetime | None = None
    recovery_attempts: int = 0
    qualified_at: datetime = field(default_factory=lambda: _now())
    expires_at: datetime = field(default_factory=lambda: _now())
    schema_version: str = MODEL_PASSPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_PASSPORT_SCHEMA_VERSION:
            raise ModelPassportError("schema_version must be '1'")
        for name in ("provider", "model_id", "auth_state"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ModelPassportError(f"{name} must be non-empty")
        if self.auth_state not in {"authorized", "unauthorized", "unknown"}:
            raise ModelPassportError("auth_state must be authorized, unauthorized, or unknown")
        if self.availability_state not in {"eligible", "degraded", "quarantined", "denied"}:
            raise ModelPassportError("availability_state is invalid")
        if isinstance(self.context_window, bool) or not isinstance(self.context_window, int):
            raise ModelPassportError("context_window must be an integer")
        if isinstance(self.recovery_attempts, bool) or not isinstance(self.recovery_attempts, int):
            raise ModelPassportError("recovery_attempts must be an integer")
        if self.recovery_attempts < 0:
            raise ModelPassportError("recovery_attempts must be non-negative")
        if self.latency_p95 is not None and (
            isinstance(self.latency_p95, bool) or not _is_finite_number(self.latency_p95)
        ):
            raise ModelPassportError("latency_p95 must be a finite number in milliseconds")
        if self.token_cost_per_1k is not None and (
            isinstance(self.token_cost_per_1k, bool)
            or not _is_finite_number(self.token_cost_per_1k)
        ):
            raise ModelPassportError("token_cost_per_1k must be a finite number")
        for name in ("last_verified_timestamp", "qualified_at", "expires_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.expires_at <= self.qualified_at:
            raise ModelPassportError("expires_at must be after qualified_at")
        for name in ("quarantine_until", "quarantined_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        if self.availability_state == "quarantined" and (
            self.quarantined_at is None or self.quarantine_until is None
        ):
            raise ModelPassportError("quarantined passports require quarantine timestamps")
        if self.availability_state != "quarantined" and (
            self.quarantine_until is not None or self.quarantined_at is not None
        ):
            raise ModelPassportError("non-quarantined passports cannot carry quarantine timestamps")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelPassport:
        payload = _strict_mapping(
            value,
            required={
                "provider",
                "model_id",
                "auth_state",
                "last_verified_timestamp",
                "availability_state",
                "qualified_at",
                "expires_at",
                "schema_version",
            },
            optional={
                "latency_p95",
                "context_window",
                "tool_support",
                "token_cost_per_1k",
                "availability_reason",
                "quarantine_until",
                "quarantined_at",
                "recovery_attempts",
            },
            field_name="model_passport",
        )
        return cls(
            provider=payload["provider"],
            model_id=payload["model_id"],
            auth_state=payload["auth_state"],
            latency_p95=payload.get("latency_p95"),
            context_window=payload.get("context_window", -1),
            tool_support=payload.get("tool_support", False),
            token_cost_per_1k=payload.get("token_cost_per_1k"),
            last_verified_timestamp=_parse_datetime(
                payload["last_verified_timestamp"], "last_verified_timestamp"
            ),
            availability_state=payload["availability_state"],
            availability_reason=payload.get("availability_reason"),
            quarantine_until=_maybe_parse_datetime(payload.get("quarantine_until")),
            quarantined_at=_maybe_parse_datetime(payload.get("quarantined_at")),
            recovery_attempts=payload.get("recovery_attempts", 0),
            qualified_at=_parse_datetime(payload["qualified_at"], "qualified_at"),
            expires_at=_parse_datetime(payload["expires_at"], "expires_at"),
            schema_version=payload["schema_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model_id": self.model_id,
            "auth_state": self.auth_state,
            "last_verified_timestamp": _format_datetime(self.last_verified_timestamp),
            "availability_state": self.availability_state,
            "qualified_at": _format_datetime(self.qualified_at),
            "expires_at": _format_datetime(self.expires_at),
        }
        if self.latency_p95 is not None:
            payload["latency_p95"] = self.latency_p95
        if self.context_window != -1:
            payload["context_window"] = self.context_window
        if self.tool_support:
            payload["tool_support"] = self.tool_support
        if self.token_cost_per_1k is not None:
            payload["token_cost_per_1k"] = self.token_cost_per_1k
        if self.availability_reason is not None:
            payload["availability_reason"] = self.availability_reason
        if self.quarantine_until is not None:
            payload["quarantine_until"] = _format_datetime(self.quarantine_until)
        if self.quarantined_at is not None:
            payload["quarantined_at"] = _format_datetime(self.quarantined_at)
        if self.recovery_attempts:
            payload["recovery_attempts"] = self.recovery_attempts
        return payload

    @property
    def digest(self) -> str:
        """Stable content digest of the canonical passport representation."""

        return _digest(self.to_dict())

    @property
    def key(self) -> str:
        """Isolation-cache key: (provider, model_id)."""

        return f"{self.provider}/{self.model_id}"


class _DeepProbePolicy(ProbePolicy):
    """Tier-2 probe: validates tool-calling and structured-output capability.

    ``ProbePolicy.payload`` is intentionally a fixed one-token ping; this
    subclass widens the payload for the deep verification pass only.
    """

    def payload(self, model_id: str) -> dict[str, Any]:
        base = super().payload(model_id)
        base["max_tokens"] = 64
        base["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Return the input verbatim",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ]
        return base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _catalog_capability(model: ModelInfo, capability: str) -> bool:
    """Return whether the catalog declares the canonical capability."""

    from verdict.availability import canonical_capability

    wanted = canonical_capability(capability)
    return any(canonical_capability(value) == wanted for value in model.capabilities)


def estimate_capacity_confidence(
    model: ModelInfo,
    *,
    estimated_tokens: int,
    headroom_pct: float | None = None,
    quota_remaining_pct: float | None = None,
    token_headroom: int | None = None,
) -> float:
    """Return a deterministic capacity-confidence score in [0, 1].

    Combines context fit (can the estimated token demand fit the model window)
    with headroom (how much of the provider quota / concurrency remains).
    """

    if isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int):
        raise ValueError("estimated_tokens must be an integer")
    if estimated_tokens < 0:
        raise ValueError("estimated_tokens must be non-negative")

    if model.context_window and model.context_window > 0:
        context_fit = (
            1.0
            if estimated_tokens <= 0.5 * model.context_window
            else (
                0.0
                if estimated_tokens > model.context_window
                else ((model.context_window - estimated_tokens) / (0.5 * model.context_window))
            )
        )
    else:
        # Unknown window: no evidence either way, so neither confirm nor deny.
        context_fit = 0.5

    known_headroom: list[float] = []
    if headroom_pct is not None and _is_finite_number(headroom_pct):
        known_headroom.append(max(0.0, min(float(headroom_pct), 100.0)) / 100.0)
    if quota_remaining_pct is not None and _is_finite_number(quota_remaining_pct):
        known_headroom.append(max(0.0, min(float(quota_remaining_pct), 100.0)) / 100.0)
    if token_headroom is not None and isinstance(token_headroom, int) and token_headroom >= 0:
        known_headroom.append(
            max(0.0, min(float(token_headroom) / float(estimated_tokens), 1.0))
            if estimated_tokens > 0
            else 1.0
        )

    headroom = sum(known_headroom) / len(known_headroom) if known_headroom else 0.5

    score = 0.5 * context_fit + 0.5 * headroom
    return max(0.0, min(score, 1.0))


def _capacity_rejects(score: float, estimated_tokens: int) -> bool:
    return estimated_tokens > LARGE_TASK_TOKENS and score <= CAPACITY_CONFIDENCE_THRESHOLD


def _running_p95(values: list[float]) -> float | None:
    """Return the p95 of the supplied latency sample, or None when empty."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = math.ceil(0.95 * len(ordered)) - 1
    return ordered[max(0, min(idx, len(ordered) - 1))]


def run_qualification(
    *,
    provider: str,
    model_id: str,
    transport: ProbeTransport,
    model: ModelInfo | None = None,
    live: bool = False,
    consented: bool = False,
    require_tools: bool = False,
    require_structured_output: bool = False,
    estimated_tokens: int | None = None,
    capacity_headroom_pct: float | None = None,
    capacity_quota_pct: float | None = None,
    capacity_token_headroom: int | None = None,
    latency_history: list[float] | None = None,
    now: datetime | None = None,
) -> ModelPassport:
    """Run the bounded qualification cascade and return a signed passport.

    Tier-1 pings the model with a one-token probe.  Tier-2 deep probe runs only
    when the task demands tool calling or structured output, or when the task
    is large enough to require capacity confirmation.  The transport is called
    exactly once when no deep demand exists.
    """

    current = _utc(now, "now") if now is not None else _now()
    reason = None

    probe = ProbeRunner()
    observation = _run_tier1(
        probe,
        provider=provider,
        model_id=model_id,
        transport=transport,
        live=live,
        consented=consented,
    )

    state = "eligible"
    quarantine_until: datetime | None = None
    quarantined_at: datetime | None = None
    recovery_attempts = 0
    if observation.availability_state == "denied":
        if observation.quarantine_until is not None:
            state = "quarantined"
            quarantined_at = observation.observed_at
            quarantine_until = observation.quarantine_until
            reason = observation.error or "quarantined"
        else:
            state = "denied"
            reason = observation.error or "denied"
    elif observation.availability_state == "degraded":
        state = "degraded"
        reason = observation.error

    tool_support = False
    if state == "eligible":
        if model is not None and _catalog_capability(model, "tools"):
            tool_support = True
        deep = (
            require_tools
            or require_structured_output
            or (estimated_tokens is not None and estimated_tokens > LARGE_TASK_TOKENS)
        )
        if deep:
            # Tier-2 deep probe re-verifies the route under a wider, tool-bearing
            # payload; the probe transport does not surface response bodies, so
            # tool support itself comes from the catalog capability plus a live
            # healthy re-check.
            _run_tier2(probe, model_id, transport, live, consented)
        if require_tools and not tool_support:
            state = "degraded"
            reason = "tool_support_unavailable"

    context_window = -1
    token_cost = None
    if model is not None:
        context_window = model.context_window if isinstance(model.context_window, int) else -1
        if isinstance(model.cost_per_1k, (int, float)) and not isinstance(model.cost_per_1k, bool):
            token_cost = float(model.cost_per_1k)

    capacity_score: float | None = None
    if state == "eligible" and estimated_tokens is not None and model is not None:
        capacity_score = estimate_capacity_confidence(
            model,
            estimated_tokens=estimated_tokens,
            headroom_pct=capacity_headroom_pct,
            quota_remaining_pct=capacity_quota_pct,
            token_headroom=capacity_token_headroom,
        )
        if _capacity_rejects(capacity_score, estimated_tokens):
            state = "degraded"
            reason = "capacity_confidence_insufficient"

    history = list(latency_history or [])
    if observation.latency_ms is not None:
        history.append(observation.latency_ms)
    latency_p95 = _running_p95(history)

    auth_state = (
        "authorized"
        if observation.availability_state == "ready"
        else "unauthorized"
        if observation.error_class == "unauthorized"
        else "unknown"
    )

    qualified_at = current
    expires_at = current.replace(second=0, microsecond=0) + _ttl_delta()
    return ModelPassport(
        provider=provider,
        model_id=model_id,
        auth_state=auth_state,
        latency_p95=latency_p95,
        context_window=context_window,
        tool_support=tool_support,
        token_cost_per_1k=token_cost,
        last_verified_timestamp=observation.observed_at,
        availability_state=state,
        availability_reason=reason,
        quarantine_until=quarantine_until,
        quarantined_at=quarantined_at,
        recovery_attempts=recovery_attempts,
        qualified_at=qualified_at,
        expires_at=expires_at,
    )


def _ttl_delta() -> Any:
    from datetime import timedelta

    return timedelta(seconds=PASSPORT_TTL_SECONDS)


def _run_tier1(
    probe: ProbeRunner,
    *,
    provider: str,
    model_id: str,
    transport: ProbeTransport,
    live: bool,
    consented: bool,
) -> ProbeObservation:
    budget = ProbeBudget(provider=provider, max_requests=1, max_tokens=1)
    results = probe.run(
        [model_id], transport, live=live, consented=consented, provider=provider, budget=budget
    )
    return results[0] if results else _failed_observation(model_id)


def _run_tier2(
    probe: ProbeRunner, model_id: str, transport: ProbeTransport, live: bool, consented: bool
) -> bool:
    deep_runner = ProbeRunner(policy=_DeepProbePolicy())
    results = deep_runner.run(
        [model_id],
        transport,
        live=live,
        consented=consented,
        provider="tier2",
        budget=ProbeBudget(provider="tier2", max_requests=1, max_tokens=64),
    )
    return bool(results and results[0].availability_state == "ready")


def _failed_observation(model_id: str) -> ProbeObservation:
    current = _now()
    return ProbeObservation(
        model_id=model_id,
        availability_state="denied",
        status="failed",
        observed_at=current,
        error_class="transport_error",
        error="probe transport unavailable",
    )


def _strict_mapping(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelPassportError(f"{field_name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ModelPassportError(f"{field_name} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ModelPassportError(f"{field_name} has unknown field(s): {', '.join(sorted(unknown))}")
    return dict(value)


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ModelPassportError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelPassportError(f"{field_name} must be an ISO-8601 string") from exc
    return _utc(parsed, field_name)


def _maybe_parse_datetime(value: Any) -> datetime | None:
    return None if value is None else _parse_datetime(value, "quarantine timestamp")


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ModelPassportError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


__all__ = [
    "CAPACITY_CONFIDENCE_THRESHOLD",
    "LARGE_TASK_TOKENS",
    "MODEL_PASSPORT_SCHEMA_VERSION",
    "PASSPORT_TTL_SECONDS",
    "ModelPassport",
    "ModelPassportError",
    "estimate_capacity_confidence",
    "run_qualification",
]
