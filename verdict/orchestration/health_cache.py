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
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    probe_class: str = "single_call"  # "agentic" | "single_call"
    agentic_ok: bool = False  # True only when a 3-turn agentic probe passed
    agentic_checked_at: datetime | None = None  # when the last agentic probe ran

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id.strip():
            raise HealthCacheError("route_id must be non-empty")
        if self.consecutive_failures < 0:
            raise HealthCacheError("consecutive_failures must be >= 0")
        _aware(self.checked_at, "checked_at")
        _aware(self.until, "until")
        if self.agentic_checked_at is not None:
            _aware(self.agentic_checked_at, "agentic_checked_at")

    def state_at(self, now: datetime) -> str:
        return classify_state(
            healthy=self.healthy, checked_at=self.checked_at, until=self.until, now=now
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "route_id": self.route_id,
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
        payload["probe_class"] = self.probe_class
        payload["agentic_ok"] = self.agentic_ok
        if self.agentic_checked_at is not None:
            payload["agentic_checked_at"] = format_datetime(self.agentic_checked_at)
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
            probe_class=str(value.get("probe_class") or "single_call"),
            agentic_ok=value.get("agentic_ok") is True,
            agentic_checked_at=(
                parse_datetime(value["agentic_checked_at"], "agentic_checked_at")
                if value.get("agentic_checked_at")
                else None
            ),
        )


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
    probe_class: str = "single_call"  # "agentic" | "single_call"
    agentic_ok: bool = False

    @property
    def healthy(self) -> bool:
        """A coding-worker success: chat and the required tool call both OK."""
        return self.chat_ok and self.tool_ok and self.category == CATEGORY_OK


@dataclass
class TokenBucket:
    """Sliding-window counter. ``zeroed_until`` is set by a 429."""

    capacity: int
    window_seconds: float
    timestamps: list[datetime]
    zeroed_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise HealthCacheError("bucket capacity must be >= 1")
        if self.window_seconds <= 0:
            raise HealthCacheError("bucket window must be positive")

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
        self.timestamps = [
            stamp for stamp in self.timestamps if _aware(stamp, "timestamp") > cutoff
        ]
        self.timestamps.extend([current] * amount)
        return True

    def zero(self, until: datetime) -> None:
        """A 429 empties the bucket until ``until``."""
        self.zeroed_until = _aware(until, "until")
        self.timestamps.clear()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "capacity": self.capacity,
            "window_seconds": self.window_seconds,
            "timestamps": [format_datetime(stamp) for stamp in self.timestamps],
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
            zeroed_until=parse_datetime(zeroed, "zeroed_until")
            if isinstance(zeroed, str)
            else None,
        )


def bucket_key(provider: str, pool: str | None = None) -> str:
    """Provider key, or ``provider/pool`` when a pool is named."""
    if not isinstance(provider, str) or not provider.strip():
        raise HealthCacheError("provider must be non-empty")
    if pool:
        return f"{provider}/{pool}"
    return provider


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


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
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
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
        self._buckets = {key: TokenBucket.from_dict(value) for key, value in buckets.items()}
        cursor = payload.get("cursor") or {}
        self._cursor = dict(cursor) if isinstance(cursor, Mapping) else {}

    def _snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": HEALTH_CACHE_SCHEMA_VERSION,
            "routes": {key: entry.to_dict() for key, entry in sorted(self._routes.items())},
            "buckets": {key: bucket.to_dict() for key, bucket in sorted(self._buckets.items())},
            "cursor": self._cursor,
        }

    def save(self) -> None:
        """Write the snapshot under an exclusive lock, replacing atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        body = json.dumps(self._snapshot(), indent=2, sort_keys=True) + "\n"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                temporary.write_text(body, encoding="utf-8")
                os.replace(temporary, self.path)
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
        if result.agentic_ok and result.probe_class == "agentic":
            # This *is* a fresh agentic probe.
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
                probe_class=probe_class,
                agentic_ok=agentic_ok,
                agentic_checked_at=agentic_checked_at,
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
                probe_class=probe_class,
                agentic_ok=False,  # failure resets agentic qualification
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

    # -- cycle cursor (partial progress) -----------------------------------

    @property
    def cursor(self) -> dict[str, Any]:
        return dict(self._cursor)

    def set_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._cursor = dict(cursor)

    def clear_cursor(self) -> None:
        self._cursor = {}


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
