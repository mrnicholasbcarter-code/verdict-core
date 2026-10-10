"""File-backed route health cache with three states and per-provider buckets.

This is the single store the prove-at-rest prober writes. The selection ladder
does not read it yet (Story 3). Readers use ``lookup`` and ``healthy_routes``.

States
------
``fresh``     checked inside the fresh window and healthy.
``stale``     past the fresh window but still inside the usable window.
``negative``  a failed probe whose ``until`` has not elapsed.
A route with no entry, or a negative whose ``until`` has elapsed, is
``unprobed``. ``unprobed`` is never healthy.

TTL policy (pure functions in ``ttl_for`` / ``next_backoff_seconds``)
----------------------------------------------------------------------
- healthy: 10 min fresh, usable-stale until 30 min;
- 429: ``Retry-After`` when given, otherwise 60 s;
- 5xx / timeout: 60 s, doubling on each consecutive failure, capped at 15 min;
- 401 / 402 / 403 / 404 / 410 / catalog_stale: 6 h, growing to 24 h;
- half-open success: the next backoff is half the previous one.

A half-open negative is a negative entry whose ``until`` is at or before
``now``. Probing it and succeeding halves the backoff instead of clearing it
to the healthy window only when the success is a recovery from failure; a
clean healthy result always uses the healthy window.

The store is one JSON file. Writers take an ``fcntl`` exclusive lock and
replace the file atomically. SQLite is not used: one daemon is the writer and
JSON contention is not measurable.

Buckets
-------
One sliding-window token bucket per provider (or ``provider/pool``). The
window defaults to 10 requests per 60 s and is overridable per provider.
A 429 zeroes the bucket until ``Retry-After``. Real calls decrement it
through ``consume``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HEALTH_CACHE_SCHEMA_VERSION = "1"

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_NEGATIVE = "negative"
STATE_UNPROBED = "unprobed"
_STORED_STATES = frozenset({STATE_FRESH, STATE_STALE, STATE_NEGATIVE})

CATEGORY_OK = "ok"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_UPSTREAM = "upstream"
CATEGORY_AUTH = "authentication"
CATEGORY_PAYMENT = "payment_required"
CATEGORY_PERMISSION = "permission"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_GONE = "gone"
CATEGORY_CATALOG_STALE = "catalog_stale"
CATEGORY_MODEL_MISMATCH = "model_mismatch"

# Status codes whose failure is about the account or the catalog, not a blip.
_LONG_TTL_STATUSES = frozenset({401, 402, 403, 404, 410})
_LONG_TTL_CATEGORIES = frozenset(
    {
        CATEGORY_AUTH,
        CATEGORY_PAYMENT,
        CATEGORY_PERMISSION,
        CATEGORY_NOT_FOUND,
        CATEGORY_GONE,
        CATEGORY_CATALOG_STALE,
    }
)

FRESH_SECONDS = 600.0
USABLE_SECONDS = 1800.0
RATE_LIMIT_SECONDS = 60.0
TRANSIENT_BASE_SECONDS = 60.0
TRANSIENT_CAP_SECONDS = 900.0
LONG_BASE_SECONDS = 6 * 3600.0
LONG_CAP_SECONDS = 24 * 3600.0

DEFAULT_BUCKET_CAPACITY = 10
DEFAULT_BUCKET_WINDOW_SECONDS = 60.0

ENV_CACHE_PATH = "VERDICT_HEALTH_CACHE"


class HealthCacheError(ValueError):
    """Raised when cache state or inputs violate the contract."""


class HealthCacheLockTimeoutError(TimeoutError):
    """Typed failure: no cache mutation ran because the lock wait expired."""

    reason = "lock_timeout"


LOCK_POLL_CAP = 40
LOCK_WAIT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.05


def _acquire_cache_lock(
    fd: int,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    start = monotonic()
    end = (
        min(start + LOCK_WAIT_SECONDS, deadline)
        if deadline is not None
        else start + LOCK_WAIT_SECONDS
    )
    poll_cap = min(LOCK_POLL_CAP, max(1, math.ceil((end - start) / LOCK_POLL_SECONDS) + 1))
    for poll in range(poll_cap):
        if monotonic() >= end:
            break
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if monotonic() >= end:
                fcntl.flock(fd, fcntl.LOCK_UN)
                break
            return
        except BlockingIOError:
            if poll + 1 < poll_cap:
                sleep(min(LOCK_POLL_SECONDS, max(0.0, end - monotonic())))
    raise HealthCacheLockTimeoutError("lock_timeout")


def default_cache_path() -> Path:
    """Configured cache path, else ``~/.verdict/health-cache.json``."""
    configured = os.getenv(ENV_CACHE_PATH)
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".verdict" / "health-cache.json").resolve()


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise HealthCacheError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def format_datetime(value: datetime) -> str:
    """Stable UTC ISO-8601 with a ``Z`` suffix."""
    return _aware(value, "timestamp").isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any, field_name: str) -> datetime:
    """Parse an ISO-8601 timestamp. Naive values are rejected."""
    if not isinstance(value, str) or not value.strip():
        raise HealthCacheError(f"{field_name} must be a non-empty ISO-8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HealthCacheError(f"{field_name} is not a valid timestamp") from exc
    return _aware(parsed, field_name)


# ---------------------------------------------------------------------------
# Pure TTL math
# ---------------------------------------------------------------------------


def is_long_negative(category: str, http_status: int | None) -> bool:
    """Auth, payment, permission and catalog failures use the long TTL."""
    if http_status in _LONG_TTL_STATUSES:
        return True
    return category in _LONG_TTL_CATEGORIES


def is_transient(category: str, http_status: int | None) -> bool:
    """5xx and timeouts use the doubling backoff."""
    if category in {CATEGORY_TIMEOUT, CATEGORY_UPSTREAM}:
        return True
    return http_status is not None and http_status >= 500


def negative_seconds(
    category: str,
    *,
    http_status: int | None = None,
    retry_after_seconds: float | None = None,
    consecutive_failures: int = 1,
) -> float:
    """How long a failed probe stays negative before it is half-open.

    ``consecutive_failures`` counts the failure being recorded (1 on the
    first failure).
    """
    failures = max(1, int(consecutive_failures))
    if category == CATEGORY_RATE_LIMITED or http_status == 429:
        if retry_after_seconds is not None and retry_after_seconds > 0:
            return float(retry_after_seconds)
        return RATE_LIMIT_SECONDS
    if is_long_negative(category, http_status):
        return _capped(LONG_BASE_SECONDS, failures, LONG_CAP_SECONDS)
    return _capped(TRANSIENT_BASE_SECONDS, failures, TRANSIENT_CAP_SECONDS)


def _capped(base: float, failures: int, cap: float) -> float:
    grown = base * float(2 ** (failures - 1))
    if grown > cap:
        return cap
    return grown


def next_backoff_seconds(previous_seconds: float) -> float:
    """Half-open success shrinks the backoff (Envoy-style), floor 60 s."""
    if previous_seconds <= 0:
        return TRANSIENT_BASE_SECONDS
    return max(TRANSIENT_BASE_SECONDS, previous_seconds / 2.0)


def fresh_until(checked_at: datetime) -> datetime:
    """End of the fresh window for a healthy check."""
    return _aware(checked_at, "checked_at") + timedelta(seconds=FRESH_SECONDS)


def usable_until(checked_at: datetime) -> datetime:
    """End of the usable-stale window for a healthy check."""
    return _aware(checked_at, "checked_at") + timedelta(seconds=USABLE_SECONDS)


def classify_state(*, healthy: bool, checked_at: datetime, until: datetime, now: datetime) -> str:
    """Map a stored entry onto ``fresh``, ``stale``, ``negative`` or ``unprobed``.

    A healthy entry is fresh until ``checked_at + 10 min``, stale until
    ``checked_at + 30 min``, then unprobed. A negative entry stays negative
    until ``until``, then becomes unprobed (half-open: eligible to probe,
    not healthy).
    """
    checked = _aware(checked_at, "checked_at")
    deadline = _aware(until, "until")
    current = _aware(now, "now")
    if healthy:
        if current < checked + timedelta(seconds=FRESH_SECONDS):
            return STATE_FRESH
        if current < checked + timedelta(seconds=USABLE_SECONDS):
            return STATE_STALE
        return STATE_UNPROBED
    if current < deadline:
        return STATE_NEGATIVE
    return STATE_UNPROBED


# ---------------------------------------------------------------------------
# Entries and buckets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthEntry:
    """One route's stored probe result. ``state`` is derived at read time."""

    route_id: str
    category: str
    checked_at: datetime
    until: datetime
    consecutive_failures: int
    chat_ok: bool
    tool_ok: bool
    latency_ms: float | None = None
    pool: str | None = None
    capacity_evidence: str | None = None
    http_status: int | None = None
    healthy: bool = False
    # Defect 5 fix: "not_reported" when the gateway did not echo the model id;
    # "verified" when the reported id matched.  Empty string means unknown/legacy.
    identity: str = ""
    probe_class: str = "single_call"  # "agentic" | "single_call"
    agentic_ok: bool = False  # True only when a 3-turn agentic probe passed
    agentic_checked_at: datetime | None = None  # when the last agentic probe ran
    # BOD-292 additive schema-1 fields. Old readers ignore them; new readers
    # accept their absence (both default to ``None``).
    # ``last_success_at`` keeps the most recent validated healthy time even
    # after a later failure overwrites ``checked_at``. It is never invented
    # from inventory or a lower-trust hint.
    last_success_at: datetime | None = None
    # ``failure_scope`` records the canonical blast radius of a failure
    # ("route" | "provider" | "pool" | "account") so a sibling-row reader can
    # tell a route-local diagnostic failure from a provider-scoped blocker.
    failure_scope: str | None = None
    session_agentic_ok: bool | None = None
    session_agentic_at: datetime | None = None
    agentic_source: str | None = None
    agentic_child_id: str | None = None
    write_revision: int = 0  # allocated only under the shared writer lock

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id.strip():
            raise HealthCacheError("route_id must be non-empty")
        if self.consecutive_failures < 0:
            raise HealthCacheError("consecutive_failures must be >= 0")
        _aware(self.checked_at, "checked_at")
        _aware(self.until, "until")
        if self.agentic_checked_at is not None:
            _aware(self.agentic_checked_at, "agentic_checked_at")
        if self.last_success_at is not None:
            _aware(self.last_success_at, "last_success_at")

    def state_at(self, now: datetime) -> str:
        return classify_state(
            healthy=self.healthy, checked_at=self.checked_at, until=self.until, now=now
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "route_id": self.route_id,
            "write_revision": self.write_revision,
            "category": self.category,
            "checked_at": format_datetime(self.checked_at),
            "until": format_datetime(self.until),
            "consecutive_failures": self.consecutive_failures,
            "chat_ok": self.chat_ok,
            "tool_ok": self.tool_ok,
            "healthy": self.healthy,
        }
        if self.latency_ms is not None:
            payload["latency_ms"] = self.latency_ms
        if self.pool is not None:
            payload["pool"] = self.pool
        if self.capacity_evidence is not None:
            payload["capacity_evidence"] = self.capacity_evidence
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.identity:
            payload["identity"] = self.identity
        payload["probe_class"] = self.probe_class
        payload["agentic_ok"] = self.agentic_ok
        if self.session_agentic_at is not None:
            payload["session_agentic_at"] = format_datetime(self.session_agentic_at)
            payload["session_agentic_ok"] = self.session_agentic_ok
        if self.agentic_source is not None:
            payload["agentic_source"] = self.agentic_source
        if self.agentic_child_id is not None:
            payload["agentic_child_id"] = self.agentic_child_id
        if self.agentic_checked_at is not None:
            payload["agentic_checked_at"] = format_datetime(self.agentic_checked_at)
        if self.last_success_at is not None:
            payload["last_success_at"] = format_datetime(self.last_success_at)
        if self.failure_scope is not None:
            payload["failure_scope"] = self.failure_scope
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HealthEntry:
        if not isinstance(value, Mapping):
            raise HealthCacheError("health entry must be a mapping")
        route_id = value.get("route_id")
        if not isinstance(route_id, str):
            raise HealthCacheError("route_id is required")
        http_status = value.get("http_status")
        latency = value.get("latency_ms")
        return cls(
            route_id=route_id,
            write_revision=int(value.get("write_revision") or 0),
            category=str(value.get("category") or ""),
            checked_at=parse_datetime(value.get("checked_at"), "checked_at"),
            until=parse_datetime(value.get("until"), "until"),
            consecutive_failures=int(value.get("consecutive_failures") or 0),
            chat_ok=value.get("chat_ok") is True,
            tool_ok=value.get("tool_ok") is True,
            latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
            pool=value.get("pool") if isinstance(value.get("pool"), str) else None,
            capacity_evidence=(
                value.get("capacity_evidence")
                if isinstance(value.get("capacity_evidence"), str)
                else None
            ),
            http_status=int(http_status) if isinstance(http_status, int) else None,
            healthy=value.get("healthy") is True,
            identity=str(value.get("identity") or ""),
            probe_class=str(value.get("probe_class") or "single_call"),
            agentic_ok=value.get("agentic_ok") is True,
            session_agentic_ok=value.get("session_agentic_ok"),
            session_agentic_at=(
                parse_datetime(value["session_agentic_at"], "session_agentic_at")
                if value.get("session_agentic_at")
                else None
            ),
            agentic_source=value.get("agentic_source"),
            agentic_child_id=value.get("agentic_child_id"),
            agentic_checked_at=(
                parse_datetime(value["agentic_checked_at"], "agentic_checked_at")
                if value.get("agentic_checked_at")
                else None
            ),
            last_success_at=(
                parse_datetime(value["last_success_at"], "last_success_at")
                if value.get("last_success_at")
                else None
            ),
            failure_scope=(
                str(value["failure_scope"])
                if isinstance(value.get("failure_scope"), str) and value["failure_scope"].strip()
                else None
            ),
        )


def agentic_capability(entry: HealthEntry | None, now: datetime) -> dict[str, Any]:
    """Capability is longer-lived session proof or the legacy short probe proof."""
    session = entry is not None and entry.session_agentic_at is not None
    checked = (entry.session_agentic_at if session else entry.agentic_checked_at) if entry else None
    ttl = FRESH_SECONDS
    if session:
        try:
            ttl = float(os.getenv("VERDICT_AGENTIC_CAPABILITY_TTL_S", str(7 * 86400)))
        except ValueError:
            ttl = 0.0
        if not math.isfinite(ttl) or ttl <= 0:
            ttl = 0.0
    passed = (entry.session_agentic_ok if session else entry.agentic_ok) if entry else False
    qualified = bool(passed and checked is not None and 0 <= (now - checked).total_seconds() <= ttl)
    return {
        "source": "session_evidence" if session else "agentic_probe",
        "child_id": entry.agentic_child_id if session and entry is not None else None,
        "checked_at": format_datetime(checked) if checked else None,
        "qualified": qualified,
        "ttl_seconds": ttl,
        "ledger": entry.agentic_source if session and entry is not None else None,
    }


@dataclass(frozen=True)
class Lookup:
    """``lookup`` result. ``entry`` is None when the route was never recorded."""

    route_id: str
    state: str
    entry: HealthEntry | None

    @property
    def healthy(self) -> bool:
        """True only for fresh or stale. Never true for negative or unprobed."""
        return self.state in {STATE_FRESH, STATE_STALE}


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one two-step probe, as recorded by ``HealthCache.record``."""

    category: str
    chat_ok: bool
    tool_ok: bool
    latency_ms: float | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    pool: str | None = None
    capacity_evidence: str | None = None
    # Defect 5 fix: "not_reported" when the gateway echoed no model id;
    # "verified" when the reported id matched the requested route.
    identity: str = ""
    probe_class: str = "single_call"  # "agentic" | "single_call"
    agentic_ok: bool = False
    # BOD-292 additive: an explicitly supplied validated success time to carry
    # forward onto a failure entry, and the canonical failure scope.
    last_success_at: datetime | None = None
    failure_scope: str | None = None

    @property
    def healthy(self) -> bool:
        """A coding-worker success: chat and the required tool call both OK."""
        return self.chat_ok and self.tool_ok and self.category == CATEGORY_OK


@dataclass(frozen=True)
class BucketReservation:
    """Opaque spend ownership. One release is allowed for this handle."""

    id: str
    bucket_key: str
    token_ids: tuple[str, ...]
    reserved_at: datetime


@dataclass
class TokenBucket:
    """Sliding-window counter. ``zeroed_until`` is set by a 429."""

    capacity: int
    window_seconds: float
    timestamps: list[datetime]
    zeroed_until: datetime | None = None
    token_ids: list[str] = field(default_factory=list)
    removed: dict[str, datetime] = field(default_factory=dict)
    released: dict[str, datetime] = field(default_factory=dict)
    ledger_at: datetime | None = None  # persisted high-water mark for pruning

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise HealthCacheError("bucket capacity must be >= 1")
        if self.window_seconds <= 0:
            raise HealthCacheError("bucket window must be positive")

    def _tokens(self) -> dict[str, datetime]:
        if len(self.token_ids) != len(self.timestamps):
            # Deterministic migration preserves multiplicity of legacy stamps.
            counts: dict[str, int] = {}
            ids: list[str] = []
            for stamp in self.timestamps:
                key = format_datetime(stamp)
                occurrence = counts.get(key, 0)
                counts[key] = occurrence + 1
                ids.append(hashlib.sha256(f"{key}:{occurrence}".encode()).hexdigest())
            self.token_ids = ids
        return dict(zip(self.token_ids, self.timestamps, strict=True))

    def _set_tokens(self, tokens: Mapping[str, datetime]) -> None:
        self.token_ids = list(tokens)
        self.timestamps = list(tokens.values())

    def remaining(self, now: datetime) -> int:
        current = _aware(now, "now")
        if self.zeroed_until is not None and current < _aware(self.zeroed_until, "zeroed_until"):
            return 0
        cutoff = current - timedelta(seconds=self.window_seconds)
        live = [stamp for stamp in self.timestamps if _aware(stamp, "timestamp") > cutoff]
        return max(0, self.capacity - len(live))

    def consume(self, now: datetime, amount: int = 1) -> bool:
        """Record ``amount`` requests. Returns False when the bucket is empty."""
        if amount < 1:
            raise HealthCacheError("consume amount must be >= 1")
        current = _aware(now, "now")
        if self.remaining(current) < amount:
            return False
        cutoff = current - timedelta(seconds=self.window_seconds)
        self.ledger_at = max(current, self.ledger_at or current)
        tokens = {key: stamp for key, stamp in self._tokens().items() if stamp > cutoff}
        tokens.update({uuid.uuid4().hex: current for _ in range(amount)})
        self._set_tokens(tokens)
        self.removed = {key: stamp for key, stamp in self.removed.items() if stamp > cutoff}
        self.released = {key: stamp for key, stamp in self.released.items() if stamp > cutoff}
        return True

    def zero(self, until: datetime) -> None:
        """A 429 empties the bucket until ``until``."""
        self.zeroed_until = _aware(until, "until")
        self.removed.update(self._tokens())
        self._set_tokens({})

    def merge_from(self, other: TokenBucket) -> None:
        """Conservative owned-token merge, also used for legacy pool migration."""
        self.capacity = min(self.capacity, other.capacity)
        self.window_seconds = max(self.window_seconds, other.window_seconds)
        tokens = other._tokens() | self._tokens()
        removed = dict(self.removed)
        released = dict(self.released)
        for target, source in ((removed, other.removed), (released, other.released)):
            for key, stamp in source.items():
                target[key] = max(stamp, target.get(key, stamp))
        stamps = [*tokens.values(), *removed.values(), *released.values()]
        stamps.extend(stamp for stamp in (self.ledger_at, other.ledger_at) if stamp is not None)
        self.ledger_at = max(stamps) if stamps else None
        cutoff = self.ledger_at - timedelta(seconds=self.window_seconds) if self.ledger_at else None
        self._set_tokens(
            {
                key: stamp
                for key, stamp in tokens.items()
                if key not in removed and (cutoff is None or stamp > cutoff)
            }
        )
        self.removed = {
            key: stamp for key, stamp in removed.items() if cutoff is None or stamp > cutoff
        }
        self.released = {
            key: stamp for key, stamp in released.items() if cutoff is None or stamp > cutoff
        }
        if other.zeroed_until is not None:
            self.zeroed_until = max(other.zeroed_until, self.zeroed_until or other.zeroed_until)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "capacity": self.capacity,
            "window_seconds": self.window_seconds,
            "timestamps": [format_datetime(stamp) for stamp in self.timestamps],
            "token_ids": list(self._tokens()),
            "removed": {key: format_datetime(stamp) for key, stamp in self.removed.items()},
            "released": {key: format_datetime(stamp) for key, stamp in self.released.items()},
            "ledger_at": format_datetime(self.ledger_at) if self.ledger_at else None,
        }
        if self.zeroed_until is not None:
            payload["zeroed_until"] = format_datetime(self.zeroed_until)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TokenBucket:
        if not isinstance(value, Mapping):
            raise HealthCacheError("bucket must be a mapping")
        raw_times = value.get("timestamps") or []
        if not isinstance(raw_times, list):
            raise HealthCacheError("bucket timestamps must be a list")
        zeroed = value.get("zeroed_until")
        return cls(
            capacity=int(value.get("capacity") or DEFAULT_BUCKET_CAPACITY),
            window_seconds=float(value.get("window_seconds") or DEFAULT_BUCKET_WINDOW_SECONDS),
            timestamps=[parse_datetime(item, "timestamp") for item in raw_times],
            token_ids=[str(item) for item in value.get("token_ids", [])],
            ledger_at=parse_datetime(value["ledger_at"], "ledger_at")
            if value.get("ledger_at")
            else None,
            removed={
                str(key): parse_datetime(stamp, "removed")
                for key, stamp in value.get("removed", {}).items()
            },
            released={
                str(key): parse_datetime(stamp, "released")
                for key, stamp in value.get("released", {}).items()
            },
            zeroed_until=parse_datetime(zeroed, "zeroed_until")
            if isinstance(zeroed, str)
            else None,
        )


@dataclass(frozen=True)
class ScopedCooldown:
    """An availability blocker scoped above a single route (BOD-292, additive).

    Stored under the optional top-level ``cooldowns`` envelope key. The key is
    a scope-bound string such as ``provider:<name>``, ``pool:<provider>/<pool>``
    or ``account:<id>``. ``category`` is the cache category; ``canonical_category``
    is the runtime classifier category (they differ by contract). Old readers
    ignore this key; new readers accept its absence.
    """

    key: str
    category: str
    checked_at: datetime
    until: datetime
    canonical_category: str | None = None
    provider_id: str | None = None
    pool: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise HealthCacheError("cooldown key must be non-empty")
        _aware(self.checked_at, "checked_at")
        _aware(self.until, "until")

    def active_at(self, now: datetime) -> bool:
        """True while the blocker is still in force (half-open at equality)."""
        return _aware(now, "now") < _aware(self.until, "until")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "category": self.category,
            "checked_at": format_datetime(self.checked_at),
            "until": format_datetime(self.until),
        }
        if self.canonical_category is not None:
            payload["canonical_category"] = self.canonical_category
        if self.provider_id is not None:
            payload["provider_id"] = self.provider_id
        if self.pool is not None:
            payload["pool"] = self.pool
        if self.account_id is not None:
            payload["account_id"] = self.account_id
        return payload

    @classmethod
    def from_dict(cls, key: str, value: Mapping[str, Any]) -> ScopedCooldown:
        if not isinstance(value, Mapping):
            raise HealthCacheError("cooldown entry must be a mapping")
        return cls(
            key=str(value.get("key") or key),
            category=str(value.get("category") or ""),
            checked_at=parse_datetime(value.get("checked_at"), "checked_at"),
            until=parse_datetime(value.get("until"), "until"),
            canonical_category=(
                str(value["canonical_category"])
                if isinstance(value.get("canonical_category"), str)
                and value["canonical_category"].strip()
                else None
            ),
            provider_id=(
                str(value["provider_id"]) if isinstance(value.get("provider_id"), str) else None
            ),
            pool=str(value["pool"]) if isinstance(value.get("pool"), str) else None,
            account_id=(
                str(value["account_id"]) if isinstance(value.get("account_id"), str) else None
            ),
        )


def bucket_key(provider: str, pool: str | None = None) -> str:
    """Canonical pool key when named, otherwise the provider key.

    Pools supplied by ``credential_pools.pool_of`` already identify the shared
    credential; adding an alias provider would split its request budget.
    """
    if not isinstance(provider, str) or not provider.strip():
        raise HealthCacheError("provider must be non-empty")
    return pool or provider


def _legacy_pool_key(key: str) -> str:
    """Recognize only exact provider/known-pool pairs, never model/user pools."""
    from verdict.orchestration.credential_pools import ALIAS_FAMILIES, pool_of
    from verdict.orchestration.provider_catalog import backend_pool

    provider, sep, pool = key.partition("/")
    if not sep or not pool or "/" in pool:
        return key
    regular = pool_of(f"{provider}/_bucket_identity_")
    free = backend_pool(f"{provider}/_bucket_identity_:free")
    if pool == regular and (regular != provider or provider in ALIAS_FAMILIES):
        return pool
    if pool == free and free != provider:
        return pool
    return key


def _canonicalize_buckets(buckets: Mapping[str, TokenBucket]) -> dict[str, TokenBucket]:
    """Fold recognized legacy keys using the same owned-token/tombstone merge."""
    canonical: dict[str, TokenBucket] = {}
    migrating = {_legacy_pool_key(key) for key in buckets if _legacy_pool_key(key) != key}
    for key, bucket in buckets.items():
        target = _legacy_pool_key(key)
        if target != key and not bucket.token_ids and bucket.timestamps:
            # Old ID-less records had no cross-bucket identity. Namespace each
            # timestamp occurrence by its source key; preserve existing IDs.
            if bucket.removed or bucket.released:
                raise HealthCacheError("ID-less legacy bucket with tombstones is ambiguous")
            bucket._tokens()
            bucket.token_ids = [
                hashlib.sha256(f"{key}:{token}".encode()).hexdigest() for token in bucket.token_ids
            ]
        if (target != key or key in migrating) and (
            len(bucket.token_ids) not in (0, len(bucket.timestamps))
            or len(set(bucket.token_ids)) != len(bucket.token_ids)
        ):
            raise HealthCacheError("partial or duplicate bucket token identity is ambiguous")
        if target in canonical:
            canonical[target].merge_from(bucket)
        else:
            canonical[target] = bucket
    return canonical


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _private_cache_mode(path: Path) -> int:
    """New caches are private; replacements never widen owner permissions."""
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return 0o600
    except OSError as exc:
        raise HealthCacheError(f"cannot read health cache: {exc}") from exc
    if not stat.S_ISREG(existing.st_mode):
        raise HealthCacheError("health cache must be a regular file")
    return stat.S_IMODE(existing.st_mode) & 0o600


def _private_atomic_write(path: Path, body: str) -> None:
    """Publish UTF-8 from a private same-directory file; caller holds the lock."""
    mode = _private_cache_mode(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(fd)
        os.close(fd)
        fd = None
        # Fail closed if a non-cooperating writer changed the destination.
        _private_cache_mode(path)
        os.replace(temporary, path)
        os.fsync(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(directory_fd)


class HealthCache:
    """JSON file store. One ``fcntl`` lock, then an atomic replace."""

    def __init__(
        self,
        path: Path,
        *,
        bucket_capacity: int = DEFAULT_BUCKET_CAPACITY,
        bucket_window_seconds: float = DEFAULT_BUCKET_WINDOW_SECONDS,
        bucket_overrides: Mapping[str, int] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        if bucket_capacity < 1 or bucket_window_seconds <= 0:
            raise HealthCacheError("default bucket bounds are invalid")
        self.bucket_capacity = bucket_capacity
        self.bucket_window_seconds = bucket_window_seconds
        self.bucket_overrides = dict(bucket_overrides or {})
        self._routes: dict[str, HealthEntry] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._cursor: dict[str, Any] = {}
        # BOD-292: optional scoped-availability blockers, keyed by scope string.
        self._cooldowns: dict[str, ScopedCooldown] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        # Reject links/FIFOs before any read can dereference or block.
        _private_cache_mode(self.path)
        if not self.path.exists():
            self._routes = {}
            self._buckets = {}
            self._cursor = {}
            self._cooldowns = {}
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HealthCacheError(f"cannot read health cache: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise HealthCacheError("health cache must be a JSON object")
        routes = payload.get("routes") or {}
        buckets = payload.get("buckets") or {}
        if not isinstance(routes, Mapping) or not isinstance(buckets, Mapping):
            raise HealthCacheError("routes and buckets must be objects")
        self._routes = {key: HealthEntry.from_dict(value) for key, value in routes.items()}
        migrating = {_legacy_pool_key(key) for key in buckets if _legacy_pool_key(key) != key}
        for key, value in buckets.items():
            if not isinstance(value, Mapping):
                raise HealthCacheError("bucket must be a mapping")
            if _legacy_pool_key(key) != key or key in migrating:
                # Never let permissive legacy defaults silently reset a named
                # quota to 10/window when its explicit bounds are invalid.
                capacity = value.get("capacity")
                window = value.get("window_seconds")
                if (
                    not isinstance(capacity, int)
                    or isinstance(capacity, bool)
                    or capacity < 1
                    or not isinstance(window, (int, float))
                    or isinstance(window, bool)
                    or not math.isfinite(window)
                    or window <= 0
                ):
                    raise HealthCacheError("legacy bucket bounds are missing or invalid")
        self._buckets = _canonicalize_buckets(
            {key: TokenBucket.from_dict(value) for key, value in buckets.items()}
        )
        cursor = payload.get("cursor") or {}
        self._cursor = dict(cursor) if isinstance(cursor, Mapping) else {}
        cooldowns = payload.get("cooldowns") or {}
        if not isinstance(cooldowns, Mapping):
            raise HealthCacheError("cooldowns must be an object")
        self._cooldowns = {
            key: ScopedCooldown.from_dict(key, value)
            for key, value in cooldowns.items()
            if isinstance(value, Mapping)
        }

    def _snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "schema_version": HEALTH_CACHE_SCHEMA_VERSION,
            "routes": {key: entry.to_dict() for key, entry in sorted(self._routes.items())},
            "buckets": {key: bucket.to_dict() for key, bucket in sorted(self._buckets.items())},
            "cursor": self._cursor,
        }
        # Additive: only emit ``cooldowns`` when non-empty so existing readers
        # and byte-for-byte goldens for cooldown-free caches stay unchanged.
        if self._cooldowns:
            snapshot["cooldowns"] = {
                key: cooldown.to_dict() for key, cooldown in sorted(self._cooldowns.items())
            }
        return snapshot

    def save(
        self,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Serialized read-merge-write: never clobber a newer concurrent write.

        BOD-292 review finding 4: a bare ``save()`` that wrote only this
        object's snapshot destroyed route health another writer (the refresh
        coordinator's ``merge_and_save``, or a second daemon) had just added.
        Every writer -- the daemon included -- now takes the same exclusive
        lock, reloads the authoritative on-disk state, merges this object's
        in-memory state onto it (newest wins), then atomically replaces the
        file. A route present only on disk is preserved; a route present in
        both requires a current write revision and a strictly newer
        ``checked_at`` (disk wins ties). Scoped cooldowns keep the later
        deadline. Buckets union owned token ids minus release tombstones,
        pruned only against the persisted ledger high-water mark.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            _acquire_cache_lock(
                handle.fileno(), deadline=deadline, monotonic=monotonic, sleep=sleep
            )
            try:
                self._merge_in_memory_over_disk()
                body = json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n"
                _private_atomic_write(self.path, body)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _merge_in_memory_over_disk(self) -> None:
        """Reload disk and fold this object's in-memory state over it.

        Must run while the exclusive lock is held. Keeps every disk-only entry
        and, for keys present in both, revision-current strictly newer proof,
        later cooldown, and owned-token union. The in-memory cursor wins (it is the
        saver's own partial-progress intent).
        """
        mine_routes = dict(self._routes)
        mine_buckets = _canonicalize_buckets(self._buckets)
        mine_cooldowns = dict(self._cooldowns)
        mine_cursor = dict(self._cursor)
        # Overwrite self with the authoritative on-disk view, then merge mine in.
        self._load()
        for route_id, entry in mine_routes.items():
            disk = self._routes.get(route_id)
            if disk is None or (
                entry.write_revision >= disk.write_revision and entry.checked_at > disk.checked_at
            ):
                self._routes[route_id] = replace(
                    entry, write_revision=(disk.write_revision if disk else 0) + 1
                )
        for cooldown in mine_cooldowns.values():
            self.record_cooldown(cooldown)
        for key, bucket in mine_buckets.items():
            disk_bucket = self._buckets.get(key)
            if disk_bucket is None:
                self._buckets[key] = bucket
                continue
            disk_bucket.merge_from(bucket)
        self._cursor = mine_cursor

    def merge_and_save(
        self,
        mutate: Callable[[HealthCache], None],
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Serialized read-modify-write under the existing exclusive lock.

        The coordinator's single writer calls this so a concurrent prober or a
        joined worker cannot clobber each other: take the lock, reload the file
        from disk into this instance (discarding only the on-disk view, not
        pending in-memory reservations held elsewhere), apply ``mutate`` to
        merge the new probe outcome/cooldown, then atomically replace the file.

        ``mutate`` runs while the lock is held and must only touch this cache.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            _acquire_cache_lock(
                handle.fileno(), deadline=deadline, monotonic=monotonic, sleep=sleep
            )
            try:
                # Reload the authoritative on-disk state so a probe written by
                # another process since we last saved is not lost.
                self._load()
                previous = dict(self._routes)
                mutate(self)
                for route_id, entry in self._routes.items():
                    prior = previous.get(route_id)
                    if entry != prior:
                        self._routes[route_id] = replace(
                            entry, write_revision=(prior.write_revision if prior else 0) + 1
                        )
                body = json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n"
                _private_atomic_write(self.path, body)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- read API ----------------------------------------------------------

    def lookup(self, route: str, now: datetime) -> Lookup:
        """State and evidence for one route at ``now``.

        Missing routes and expired negatives are ``unprobed``. ``unprobed``
        is never reported as healthy.
        """
        current = _aware(now, "now")
        entry = self._routes.get(route)
        if entry is None:
            return Lookup(route_id=route, state=STATE_UNPROBED, entry=None)
        return Lookup(route_id=route, state=entry.state_at(current), entry=entry)

    def healthy_routes(
        self, now: datetime, predicate: Callable[[HealthEntry], bool] | None = None
    ) -> tuple[HealthEntry, ...]:
        """Fresh or stale entries that pass ``predicate``, oldest check last.

        Negatives and unprobed routes are excluded. Story 3's ladder is the
        intended caller.
        """
        current = _aware(now, "now")
        found: list[HealthEntry] = []
        for entry in self._routes.values():
            if entry.state_at(current) not in {STATE_FRESH, STATE_STALE}:
                continue
            if predicate is not None and not predicate(entry):
                continue
            found.append(entry)
        found.sort(key=lambda item: item.checked_at, reverse=True)
        return tuple(found)

    def entry(self, route: str) -> HealthEntry | None:
        return self._routes.get(route)

    def routes(self) -> Mapping[str, HealthEntry]:
        return dict(self._routes)

    # -- write API ---------------------------------------------------------

    def record_agentic_evidence(
        self, route: str, *, passed: bool, at: datetime, source: str, child_id: str | None = None
    ) -> bool:
        """Update exact-route qualification, never invent or refresh liveness.

        Called under ``merge_and_save``. Older evidence cannot replace newer
        agentic results; a failure wins conflicting evidence at the same time.
        """
        current = _aware(at, "agentic_checked_at")
        previous = self._routes.get(route)
        prior_at = (
            (previous.session_agentic_at or previous.agentic_checked_at) if previous else None
        )
        prior_ok = (
            (previous.session_agentic_ok if previous.session_agentic_at else previous.agentic_ok)
            if previous
            else None
        )
        if prior_at is not None:
            if current < prior_at:
                return False
            if current == prior_at and (passed or not prior_ok):
                return False
        base = previous or HealthEntry(
            route_id=route,
            category="session_evidence",
            checked_at=current,
            until=current,
            consecutive_failures=0,
            chat_ok=False,
            tool_ok=False,
        )
        self._routes[route] = replace(
            base,
            probe_class="agentic",
            agentic_ok=passed,
            agentic_checked_at=current,
            agentic_source=source,
            agentic_child_id=child_id,
            session_agentic_ok=passed,
            session_agentic_at=current,
        )
        return True

    def record(self, route: str, result: ProbeResult, now: datetime) -> HealthEntry:
        """Record one probe and return the stored entry. Does not save."""
        if not isinstance(route, str) or not route.strip():
            raise HealthCacheError("route must be non-empty")
        current = _aware(now, "now")
        previous = self._routes.get(route)
        # Preserve the best probe_class: an existing agentic pass is kept even
        # when a subsequent single_call probe runs (agentic is rarer).
        probe_class = result.probe_class
        agentic_ok = result.agentic_ok
        # Track agentic timestamp independently from checked_at so that
        # repeated single-call PASSes cannot keep an old agentic PASS fresh.
        agentic_checked_at: datetime | None = None
        if result.probe_class == "agentic":
            # This *is* a fresh agentic attempt, pass or fail. Recording the
            # time on failures too keeps a failing route to one retry per
            # interval.
            agentic_checked_at = current
        if previous is not None and previous.agentic_ok and probe_class == "single_call":
            agentic_ok = True
            probe_class = "agentic"
            # Carry forward the *original* agentic timestamp, NOT the
            # current single-call timestamp.
            agentic_checked_at = previous.agentic_checked_at
        if result.healthy:
            entry = HealthEntry(
                route_id=route,
                category=CATEGORY_OK,
                checked_at=current,
                until=usable_until(current),
                consecutive_failures=0,
                chat_ok=True,
                tool_ok=True,
                latency_ms=result.latency_ms,
                pool=result.pool,
                capacity_evidence=result.capacity_evidence,
                http_status=result.http_status,
                healthy=True,
                identity=result.identity,
                probe_class=probe_class,
                agentic_ok=agentic_ok,
                agentic_checked_at=agentic_checked_at,
                # A fresh success is itself the latest validated success time.
                last_success_at=current,
                failure_scope=None,
            )
        else:
            prior = previous.consecutive_failures if previous is not None else 0
            failures = prior + 1
            seconds = negative_seconds(
                result.category,
                http_status=result.http_status,
                retry_after_seconds=result.retry_after_seconds,
                consecutive_failures=failures,
            )
            if (
                previous is not None
                and not previous.healthy
                and previous.state_at(current) == STATE_UNPROBED
                and result.chat_ok
                and not result.tool_ok
            ):
                # A half-open probe that only half-succeeds still counts as a
                # failure; backoff above already doubled.
                pass
            entry = HealthEntry(
                route_id=route,
                category=result.category,
                checked_at=current,
                until=current + timedelta(seconds=seconds),
                consecutive_failures=failures,
                chat_ok=result.chat_ok,
                tool_ok=result.tool_ok,
                latency_ms=result.latency_ms,
                pool=result.pool
                if result.pool is not None
                else (previous.pool if previous is not None else None),
                capacity_evidence=result.capacity_evidence,
                http_status=result.http_status,
                healthy=False,
                identity=result.identity,
                probe_class=probe_class,
                agentic_ok=False,  # failure resets agentic qualification
                agentic_checked_at=(
                    agentic_checked_at
                    if agentic_checked_at is not None
                    else (previous.agentic_checked_at if previous is not None else None)
                ),
                # Preserve the earlier valid success time (never invent one).
                last_success_at=(
                    result.last_success_at
                    if result.last_success_at is not None
                    else (previous.last_success_at if previous is not None else None)
                ),
                failure_scope=result.failure_scope,
            )
        entry = replace(
            entry,
            write_revision=previous.write_revision if previous else 0,
            agentic_source=previous.agentic_source if previous else None,
            agentic_child_id=previous.agentic_child_id if previous else None,
            session_agentic_ok=previous.session_agentic_ok if previous else None,
            session_agentic_at=previous.session_agentic_at if previous else None,
        )
        if (
            result.probe_class == "agentic"
            and result.chat_ok
            and not result.agentic_ok
            and entry.session_agentic_at is not None
            and entry.session_agentic_at <= current
        ):
            # The route answered but failed a newer exact-route agentic probe:
            # durably revoke older session proof. Later single-call or
            # liveness probes must not resurrect it. Quota/transport failures
            # (chat_ok False) say nothing about capability and do not revoke.
            entry = replace(
                entry,
                session_agentic_ok=False,
                session_agentic_at=current,
                agentic_source="agentic_probe",
                agentic_child_id=None,
            )
        self._routes[route] = entry
        return entry

    def record_liveness(
        self,
        route: str,
        *,
        latency_ms: float | None,
        pool: str | None,
        capacity_evidence: str | None,
        now: datetime,
        identity: str = "",
    ) -> HealthEntry:
        """Chat-only success for a non-FREE route.

        The entry is healthy (fresh, then stale) so the prober does not
        re-check it immediately, but ``tool_ok`` is false: it is not a coding
        worker. ``healthy_routes`` with a tool predicate excludes it.
        """
        if not isinstance(route, str) or not route.strip():
            raise HealthCacheError("route must be non-empty")
        current = _aware(now, "now")
        entry = HealthEntry(
            route_id=route,
            category=CATEGORY_OK,
            checked_at=current,
            until=usable_until(current),
            consecutive_failures=0,
            chat_ok=True,
            tool_ok=False,
            latency_ms=latency_ms,
            pool=pool,
            capacity_evidence=capacity_evidence,
            http_status=200,
            healthy=True,
            identity=identity,
            last_success_at=current,
            session_agentic_ok=(
                self._routes[route].session_agentic_ok if route in self._routes else None
            ),
            session_agentic_at=(
                self._routes[route].session_agentic_at if route in self._routes else None
            ),
            probe_class=(
                self._routes[route].probe_class if route in self._routes else "single_call"
            ),
            agentic_ok=(self._routes[route].agentic_ok if route in self._routes else False),
            agentic_checked_at=(
                self._routes[route].agentic_checked_at if route in self._routes else None
            ),
            agentic_source=(self._routes[route].agentic_source if route in self._routes else None),
            agentic_child_id=(
                self._routes[route].agentic_child_id if route in self._routes else None
            ),
            write_revision=(self._routes[route].write_revision if route in self._routes else 0),
        )
        self._routes[route] = entry
        return entry

    def record_half_open_success(self, route: str, now: datetime) -> float:
        """Halve the negative backoff after a half-open probe succeeds.

        Returns the new backoff in seconds. Used when a probe of an expired
        negative succeeds but the caller wants the shrunk backoff recorded as
        ``capacity_evidence`` context. The stored entry is still healthy.
        """
        previous = self._routes.get(route)
        if previous is None or previous.healthy:
            return 0.0
        span = (previous.until - previous.checked_at).total_seconds()
        return next_backoff_seconds(span)

    # -- buckets -----------------------------------------------------------

    def _bucket_capacity_for(self, key: str) -> int:
        if key in self.bucket_overrides:
            return self.bucket_overrides[key]
        provider = key.split("/", 1)[0]
        return self.bucket_overrides.get(provider, self.bucket_capacity)

    def bucket_for(self, provider: str, pool: str | None = None) -> TokenBucket:
        """Return the bucket, creating it from the default or the override."""
        key = bucket_key(provider, pool)
        found = self._buckets.get(key)
        if found is None:
            found = TokenBucket(
                capacity=self._bucket_capacity_for(key),
                window_seconds=self.bucket_window_seconds,
                timestamps=[],
            )
            self._buckets[key] = found
        return found

    def consume(
        self, provider: str, now: datetime, *, pool: str | None = None, amount: int = 1
    ) -> bool:
        """Decrement the provider/pool bucket. False when it is empty."""
        bucket = self.bucket_for(provider, pool)
        return bucket.consume(now, amount)

    def zero_bucket(self, provider: str, until: datetime, *, pool: str | None = None) -> None:
        """A 429 empties the provider/pool bucket until ``until``."""
        self.bucket_for(provider, pool).zero(until)

    def bucket_remaining(self, provider: str, now: datetime, *, pool: str | None = None) -> int:
        return self.bucket_for(provider, pool).remaining(now)

    def reserve_bucket(
        self,
        provider: str,
        now: datetime,
        *,
        pool: str | None = None,
        amount: int = 1,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> BucketReservation | None:
        """Persist owned tokens before dispatch. None means no spend allowed."""
        reservation: BucketReservation | None = None

        def _mutate(cache: HealthCache) -> None:
            nonlocal reservation
            bucket = cache.bucket_for(provider, pool)
            before = set(bucket._tokens())
            if bucket.consume(now, amount):
                ids = tuple(key for key in bucket.token_ids if key not in before)
                reservation = BucketReservation(
                    uuid.uuid4().hex, bucket_key(provider, pool), ids, now
                )

        self.merge_and_save(_mutate, deadline=deadline, monotonic=monotonic, sleep=sleep)
        return reservation

    def release_bucket(
        self,
        handle: BucketReservation,
        unused_n: int,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Release only this handle's unused tokens; repeat release is a no-op."""
        if not isinstance(handle, BucketReservation):
            raise HealthCacheError("release requires a reservation handle")
        if unused_n < 0 or unused_n > len(handle.token_ids):
            raise HealthCacheError("unused_n exceeds the owned reservation")

        def _mutate(cache: HealthCache) -> None:
            bucket = cache._buckets.get(handle.bucket_key)
            if bucket is None or handle.id in bucket.released:
                return
            tokens = bucket._tokens()
            bucket.released[handle.id] = handle.reserved_at
            for token in handle.token_ids[:unused_n]:
                stamp = tokens.pop(token, None)
                if stamp is not None:
                    bucket.removed[token] = stamp
            bucket._set_tokens(tokens)

        self.merge_and_save(_mutate, deadline=deadline, monotonic=monotonic, sleep=sleep)

    # -- cycle cursor (partial progress) -----------------------------------

    @property
    def cursor(self) -> dict[str, Any]:
        return dict(self._cursor)

    def set_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._cursor = dict(cursor)

    def clear_cursor(self) -> None:
        self._cursor = {}

    # -- scoped cooldowns (BOD-292, additive) ------------------------------

    def cooldowns(self) -> Mapping[str, ScopedCooldown]:
        """Every stored scoped cooldown (active or expired)."""
        return dict(self._cooldowns)

    def active_cooldowns(self, now: datetime) -> tuple[ScopedCooldown, ...]:
        """Scoped cooldowns still in force at ``now``."""
        return tuple(c for c in self._cooldowns.values() if c.active_at(now))

    def cooldown_for(self, key: str, now: datetime) -> ScopedCooldown | None:
        """The active cooldown under ``key``, or None when absent/expired."""
        found = self._cooldowns.get(key)
        if found is None or not found.active_at(now):
            return None
        return found

    def record_cooldown(self, cooldown: ScopedCooldown) -> None:
        """Store a scoped blocker, keeping the later deadline on conflict.

        A sibling route's success never deletes a provider/pool/account
        cooldown; only a later deadline for the same key replaces it. Does not
        save; the caller serializes the write.
        """
        existing = self._cooldowns.get(cooldown.key)
        if existing is not None and existing.until >= cooldown.until:
            return
        self._cooldowns[cooldown.key] = cooldown

    def clear_cooldown(self, key: str) -> None:
        """Explicitly drop a scoped blocker after a deliberate scope recovery."""
        self._cooldowns.pop(key, None)


__all__ = [
    "CATEGORY_AUTH",
    "CATEGORY_CATALOG_STALE",
    "CATEGORY_GONE",
    "CATEGORY_NOT_FOUND",
    "CATEGORY_OK",
    "CATEGORY_PAYMENT",
    "CATEGORY_PERMISSION",
    "CATEGORY_RATE_LIMITED",
    "CATEGORY_TIMEOUT",
    "CATEGORY_UPSTREAM",
    "DEFAULT_BUCKET_CAPACITY",
    "DEFAULT_BUCKET_WINDOW_SECONDS",
    "ENV_CACHE_PATH",
    "FRESH_SECONDS",
    "HEALTH_CACHE_SCHEMA_VERSION",
    "LONG_BASE_SECONDS",
    "LONG_CAP_SECONDS",
    "RATE_LIMIT_SECONDS",
    "STATE_FRESH",
    "STATE_NEGATIVE",
    "STATE_STALE",
    "STATE_UNPROBED",
    "TRANSIENT_BASE_SECONDS",
    "TRANSIENT_CAP_SECONDS",
    "USABLE_SECONDS",
    "HealthCache",
    "HealthCacheError",
    "HealthEntry",
    "Lookup",
    "ProbeResult",
    "ScopedCooldown",
    "TokenBucket",
    "bucket_key",
    "classify_state",
    "default_cache_path",
    "format_datetime",
    "fresh_until",
    "is_long_negative",
    "is_transient",
    "negative_seconds",
    "next_backoff_seconds",
    "parse_datetime",
    "usable_until",
]
