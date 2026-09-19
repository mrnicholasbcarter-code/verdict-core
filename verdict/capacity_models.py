"""Live capacity evidence contracts (BOD-129).

Evidence producers only — never routing, economics, or recovery authority.
Projects into BOD-92 / BOD-54 / BOD-104 consumer shapes via capacity_project.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

CAPACITY_CONTRACT_VERSION = "1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

# Mirror gateway_adapters secret rejection; capacity receipts must stay secret-free.
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

AccessMode = Literal[
    "api_key",
    "oauth",
    "web_session",
    "no_auth",
    "local",
    "upstream_proxy",
    "unknown",
]
PoolUnit = Literal["tokens", "requests", "credits", "usd", "compute", "unknown"]
PoolStatus = Literal["available", "constrained", "exhausted", "cooldown", "unknown"]
SourceKind = Literal[
    "direct_provider",
    "gateway",
    "aggregator",
    "local_history",
    "unknown",
]
BalanceKind = Literal[
    "prepaid_usd",
    "provider_credits",
    "subscription_credits",
    "reset_credits",
    "overage_budget",
    "unknown",
]


class CapacityEvidenceError(ValueError):
    """Raised when capacity evidence is malformed, ambiguous, or secret-bearing."""


class EvidenceAuthority(str, Enum):
    """Default evidence-authority ordering (not a routing preference).

    Lower ``rank`` is higher authority. Freshness and account identity still
    participate in conflict resolution — stale official evidence does not beat
    fresh direct evidence for a different account.
    """

    PROVIDER_API = "provider_api"
    OFFICIAL_CLI = "official_cli"
    FIRST_PARTY_UNDOCUMENTED = "first_party_undocumented"
    GATEWAY_NATIVE = "gateway_native"
    AGGREGATOR = "aggregator"
    LOCAL_HISTORY = "local_history"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        order = (
            EvidenceAuthority.PROVIDER_API,
            EvidenceAuthority.OFFICIAL_CLI,
            EvidenceAuthority.FIRST_PARTY_UNDOCUMENTED,
            EvidenceAuthority.GATEWAY_NATIVE,
            EvidenceAuthority.AGGREGATOR,
            EvidenceAuthority.LOCAL_HISTORY,
            EvidenceAuthority.UNKNOWN,
        )
        return order.index(self)


class CapacitySignal(str, Enum):
    """Signals an adapter may claim via capabilities()."""

    QUOTA_WINDOWS = "quota_windows"
    BALANCES = "balances"
    RESET_TIMES = "reset_times"
    COOLDOWNS = "cooldowns"
    RETRY_AFTER = "retry_after"
    SHARED_POOLS = "shared_pools"
    HEALTH = "health"


class CapacityFailureClass(str, Enum):
    """Normalized capacity observation failure classes.

    Distinct from routing exclusions. HTTP 429 is RATE_LIMIT (temporary),
    not QUOTA_EXHAUSTED, unless explicit exhaustion evidence is present.
    """

    RATE_LIMIT = "rate_limit"
    CONCURRENCY_LIMIT = "concurrency_limit"
    PROVIDER_OVERLOAD = "provider_overload"
    QUOTA_EXHAUSTED = "quota_exhausted"
    BALANCE_EXHAUSTED = "balance_exhausted"
    AUTH_EXPIRED = "auth_expired"
    PERMISSION_DENIED = "permission_denied"
    TRANSPORT = "transport"
    PROVIDER_OUTAGE = "provider_outage"
    UNSUPPORTED = "unsupported"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise CapacityEvidenceError("datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _reject_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CapacityEvidenceError(f"{path} keys must be strings")
            normalized = key.lower()
            if normalized in _SECRET_NAMES or normalized.endswith(
                ("_api_key", "_password", "_secret", "_token")
            ):
                raise CapacityEvidenceError(f"secret-bearing field rejected: {path}.{key}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _non_empty(name: str, value: str) -> str:
    text = str(value).strip()
    if not text:
        raise CapacityEvidenceError(f"{name} must be a non-empty string")
    if name in {"adapter_id", "pool_id", "provider_id", "balance_id"} and not re.match(
        r"^[a-zA-Z0-9][a-zA-Z0-9_.:/@+-]*$", text
    ):
        raise CapacityEvidenceError(f"{name} has invalid characters")
    return text


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _optional_pct(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapacityEvidenceError(f"{name} must be a number or null")
    number = float(value)
    if number != number:  # NaN
        raise CapacityEvidenceError(f"{name} must be finite")
    if number < 0.0 or number > 100.0:
        raise CapacityEvidenceError(f"{name} must be between 0 and 100")
    return number


@dataclass(frozen=True)
class ConnectionIdentity:
    """Concrete account/resource identity — not merely a provider name."""

    provider_id: str
    account_id: str
    adapter_id: str
    access_mode: AccessMode = "unknown"
    gateway_id: str | None = None
    workspace_id: str | None = None
    execution_plane: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _non_empty("provider_id", self.provider_id))
        object.__setattr__(self, "account_id", str(self.account_id).strip())
        if not self.account_id:
            raise CapacityEvidenceError("account_id must be a non-empty string")
        # account_id is a stable redacted fingerprint — never a raw credential.
        lowered = self.account_id.lower()
        if any(marker in lowered for marker in ("sk-", "bearer ", "eyj", "cookie=")):
            raise CapacityEvidenceError("account_id must not contain credential material")
        object.__setattr__(self, "adapter_id", _non_empty("adapter_id", self.adapter_id))
        if self.access_mode not in {
            "api_key",
            "oauth",
            "web_session",
            "no_auth",
            "local",
            "upstream_proxy",
            "unknown",
        }:
            raise CapacityEvidenceError("access_mode is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "access_mode": self.access_mode,
            "gateway_id": self.gateway_id,
            "workspace_id": self.workspace_id,
            "execution_plane": self.execution_plane,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConnectionIdentity:
        _reject_secrets(value, "connection")
        return cls(
            provider_id=str(value["provider_id"]),
            account_id=str(value["account_id"]),
            adapter_id=str(value["adapter_id"]),
            access_mode=value.get("access_mode", "unknown"),  # type: ignore[arg-type]
            gateway_id=None if value.get("gateway_id") is None else str(value["gateway_id"]),
            workspace_id=(
                None if value.get("workspace_id") is None else str(value["workspace_id"])
            ),
            execution_plane=(
                None if value.get("execution_plane") is None else str(value["execution_plane"])
            ),
        )


@dataclass(frozen=True)
class CapacityPool:
    """One independently constrained or shared capacity window."""

    pool_id: str
    status: PoolStatus = "unknown"
    unit: PoolUnit = "unknown"
    scope: str | None = None
    remaining_pct: float | None = None
    used_pct: float | None = None
    remaining_units: float | None = None
    limit_units: float | None = None
    reset_at: datetime | None = None
    window_seconds: int | None = None
    shared_pool_id: str | None = None
    hard_limit: bool | None = None
    overage_allowed: bool | None = None
    cooldown_until: datetime | None = None
    retry_after_seconds: int | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", _non_empty("pool_id", self.pool_id))
        object.__setattr__(self, "remaining_pct", _optional_pct("remaining_pct", self.remaining_pct))
        object.__setattr__(self, "used_pct", _optional_pct("used_pct", self.used_pct))
        if self.status not in {
            "available",
            "constrained",
            "exhausted",
            "cooldown",
            "unknown",
        }:
            raise CapacityEvidenceError("status is invalid")
        if self.unit not in {"tokens", "requests", "credits", "usd", "compute", "unknown"}:
            raise CapacityEvidenceError("unit is invalid")
        if self.window_seconds is not None and self.window_seconds < 0:
            raise CapacityEvidenceError("window_seconds must be >= 0")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise CapacityEvidenceError("retry_after_seconds must be >= 0")
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", _utc(self.reset_at))
        if self.cooldown_until is not None:
            object.__setattr__(self, "cooldown_until", _utc(self.cooldown_until))

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "status": self.status,
            "unit": self.unit,
            "scope": self.scope,
            "remaining_pct": self.remaining_pct,
            "used_pct": self.used_pct,
            "remaining_units": self.remaining_units,
            "limit_units": self.limit_units,
            "reset_at": None if self.reset_at is None else _format_datetime(self.reset_at),
            "window_seconds": self.window_seconds,
            "shared_pool_id": self.shared_pool_id,
            "hard_limit": self.hard_limit,
            "overage_allowed": self.overage_allowed,
            "cooldown_until": (
                None if self.cooldown_until is None else _format_datetime(self.cooldown_until)
            ),
            "retry_after_seconds": self.retry_after_seconds,
            "evidence_id": self.evidence_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapacityPool:
        _reject_secrets(value, "pool")

        def _parse_dt(raw: Any) -> datetime | None:
            if raw is None:
                return None
            text = str(raw).replace("Z", "+00:00")
            return datetime.fromisoformat(text)

        return cls(
            pool_id=str(value["pool_id"]),
            status=value.get("status", "unknown"),  # type: ignore[arg-type]
            unit=value.get("unit", "unknown"),  # type: ignore[arg-type]
            scope=None if value.get("scope") is None else str(value["scope"]),
            remaining_pct=value.get("remaining_pct"),
            used_pct=value.get("used_pct"),
            remaining_units=value.get("remaining_units"),
            limit_units=value.get("limit_units"),
            reset_at=_parse_dt(value.get("reset_at")),
            window_seconds=value.get("window_seconds"),
            shared_pool_id=(
                None if value.get("shared_pool_id") is None else str(value["shared_pool_id"])
            ),
            hard_limit=value.get("hard_limit"),
            overage_allowed=value.get("overage_allowed"),
            cooldown_until=_parse_dt(value.get("cooldown_until")),
            retry_after_seconds=value.get("retry_after_seconds"),
            evidence_id=None if value.get("evidence_id") is None else str(value["evidence_id"]),
        )


@dataclass(frozen=True)
class Balance:
    """Monetary/credit balance distinct from rolling quota windows."""

    balance_id: str
    kind: BalanceKind = "unknown"
    remaining: float | None = None
    currency_or_unit: str | None = None
    resets: bool | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "balance_id", _non_empty("balance_id", self.balance_id))
        if self.kind not in {
            "prepaid_usd",
            "provider_credits",
            "subscription_credits",
            "reset_credits",
            "overage_budget",
            "unknown",
        }:
            raise CapacityEvidenceError("balance kind is invalid")
        # Do not invent USD conversions — remaining stays in provider units.

    def to_dict(self) -> dict[str, Any]:
        return {
            "balance_id": self.balance_id,
            "kind": self.kind,
            "remaining": self.remaining,
            "currency_or_unit": self.currency_or_unit,
            "resets": self.resets,
            "evidence_id": self.evidence_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Balance:
        _reject_secrets(value, "balance")
        return cls(
            balance_id=str(value["balance_id"]),
            kind=value.get("kind", "unknown"),  # type: ignore[arg-type]
            remaining=value.get("remaining"),
            currency_or_unit=(
                None if value.get("currency_or_unit") is None else str(value["currency_or_unit"])
            ),
            resets=value.get("resets"),
            evidence_id=None if value.get("evidence_id") is None else str(value["evidence_id"]),
        )


@dataclass(frozen=True)
class CapacityObservationError:
    """Explicit parse/transport/unsupported error — never silent fabrication."""

    failure_class: CapacityFailureClass
    message: str
    retry_after_seconds: int | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        if not str(self.message).strip():
            raise CapacityEvidenceError("error message must be non-empty")
        object.__setattr__(self, "message", str(self.message).strip())
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise CapacityEvidenceError("retry_after_seconds must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "message": self.message,
            "retry_after_seconds": self.retry_after_seconds,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class CapacitySnapshot:
    """One normalized capacity observation with provenance and freshness."""

    identity: ConnectionIdentity
    source_kind: SourceKind
    authority: EvidenceAuthority
    observed_at: datetime
    pools: tuple[CapacityPool, ...] = ()
    balances: tuple[Balance, ...] = ()
    fresh_until: datetime | None = None
    confidence: float | None = None
    health: str | None = None
    errors: tuple[CapacityObservationError, ...] = ()
    evidence_digest: str = ""
    contract_version: str = CAPACITY_CONTRACT_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.contract_version != CAPACITY_CONTRACT_VERSION:
            raise CapacityEvidenceError("contract_version must be '1'")
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if self.fresh_until is not None:
            object.__setattr__(self, "fresh_until", _utc(self.fresh_until))
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "balances", tuple(self.balances))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "notes", tuple(self.notes))
        if self.confidence is not None and not (0.0 <= float(self.confidence) <= 1.0):
            raise CapacityEvidenceError("confidence must be between 0 and 1")
        if self.source_kind not in {
            "direct_provider",
            "gateway",
            "aggregator",
            "local_history",
            "unknown",
        }:
            raise CapacityEvidenceError("source_kind is invalid")
        payload = {
            "identity": self.identity.to_dict(),
            "source_kind": self.source_kind,
            "authority": self.authority.value,
            "observed_at": _format_datetime(self.observed_at),
            "pools": [pool.to_dict() for pool in self.pools],
            "balances": [balance.to_dict() for balance in self.balances],
            "errors": [error.to_dict() for error in self.errors],
        }
        _reject_secrets(payload, "snapshot")
        if not self.evidence_digest:
            object.__setattr__(self, "evidence_digest", _digest(payload))
        elif not _DIGEST.match(self.evidence_digest):
            raise CapacityEvidenceError("evidence_digest must be sha256 hex")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "identity": self.identity.to_dict(),
            "source_kind": self.source_kind,
            "authority": self.authority.value,
            "observed_at": _format_datetime(self.observed_at),
            "fresh_until": None if self.fresh_until is None else _format_datetime(self.fresh_until),
            "confidence": self.confidence,
            "health": self.health,
            "pools": [pool.to_dict() for pool in self.pools],
            "balances": [balance.to_dict() for balance in self.balances],
            "errors": [error.to_dict() for error in self.errors],
            "evidence_digest": self.evidence_digest,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapacitySnapshot:
        _reject_secrets(value, "snapshot")

        def _parse_dt(raw: Any) -> datetime | None:
            if raw is None:
                return None
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))

        errors = tuple(
            CapacityObservationError(
                failure_class=CapacityFailureClass(item["failure_class"]),
                message=str(item["message"]),
                retry_after_seconds=item.get("retry_after_seconds"),
                http_status=item.get("http_status"),
            )
            for item in value.get("errors", ())
        )
        return cls(
            identity=ConnectionIdentity.from_dict(value["identity"]),
            source_kind=value.get("source_kind", "unknown"),  # type: ignore[arg-type]
            authority=EvidenceAuthority(value.get("authority", "unknown")),
            observed_at=_parse_dt(value["observed_at"]) or _utc(),
            pools=tuple(CapacityPool.from_dict(item) for item in value.get("pools", ())),
            balances=tuple(Balance.from_dict(item) for item in value.get("balances", ())),
            fresh_until=_parse_dt(value.get("fresh_until")),
            confidence=value.get("confidence"),
            health=None if value.get("health") is None else str(value["health"]),
            errors=errors,
            evidence_digest=str(value.get("evidence_digest") or ""),
            contract_version=str(value.get("contract_version", CAPACITY_CONTRACT_VERSION)),
            notes=tuple(str(item) for item in value.get("notes", ())),
        )


@dataclass(frozen=True)
class RefreshPolicy:
    """TTL / reset / backoff hints — evidence only."""

    ttl_seconds: int | None = None
    prefer_reset_at: bool = True
    honor_retry_after: bool = True
    min_refresh_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ttl_seconds": self.ttl_seconds,
            "prefer_reset_at": self.prefer_reset_at,
            "honor_retry_after": self.honor_retry_after,
            "min_refresh_seconds": self.min_refresh_seconds,
        }


@dataclass(frozen=True)
class DiagnoseReport:
    """Explain unavailable / auth-expired / unsupported state without secrets."""

    adapter_id: str
    available: bool
    status: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _non_empty("adapter_id", self.adapter_id))
        details = dict(self.details)
        _reject_secrets(details, "diagnose")
        object.__setattr__(self, "details", MappingProxyType(details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "available": self.available,
            "status": self.status,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ConflictResolution:
    """Preserves both observations and records an explainable winner."""

    preferred: CapacitySnapshot
    alternate: CapacitySnapshot
    reason: str
    averaged: bool = False

    def __post_init__(self) -> None:
        if self.averaged:
            raise CapacityEvidenceError("capacity conflicts must never be averaged")
        if not str(self.reason).strip():
            raise CapacityEvidenceError("conflict reason must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred": self.preferred.to_dict(),
            "alternate": self.alternate.to_dict(),
            "reason": self.reason,
            "averaged": False,
            "both_retained": True,
        }


__all__ = [
    "CAPACITY_CONTRACT_VERSION",
    "AccessMode",
    "Balance",
    "BalanceKind",
    "CapacityEvidenceError",
    "CapacityFailureClass",
    "CapacityObservationError",
    "CapacityPool",
    "CapacitySignal",
    "CapacitySnapshot",
    "ConflictResolution",
    "ConnectionIdentity",
    "DiagnoseReport",
    "EvidenceAuthority",
    "PoolStatus",
    "PoolUnit",
    "RefreshPolicy",
    "SourceKind",
]
