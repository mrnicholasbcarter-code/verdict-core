"""Bounded availability cache with documented TTL and stale-while-revalidate.

The cache wraps an :class:`OmniRouteAvailabilityAdapter` (or any callable that
returns an :class:`AvailabilityReport`).  It deliberately never trusts a stale
entry for protected routing unless an explicit policy opt-in allows degraded
mode, and it isolates entries per provider/model/policy-version so one
source can never poison another.

Design constraints (from issue #56 acceptance criteria):
- Cache keys include provider/model and the relevant policy/contract version.
- Expired or refresh-failed states are explicit ``unknown``/``error`` states.
- Concurrent refreshes are bounded and deduplicated.
- ``explain`` output identifies freshness and source.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from verdict.availability import AvailabilityReport, _now


@dataclass(frozen=True)
class CacheKey:
    """Identity for a cached availability observation.

    Includes provider/model and the policy/contract version so a contract
    change invalidates stale entries instead of leaking them across versions.
    """

    provider: str
    model: str
    policy_version: str = "policy-2026-07-13.1"

    @classmethod
    def for_candidate(cls, model_id: str, policy_version: str | None = None) -> CacheKey:
        provider = model_id.split("/", 1)[0] if "/" in model_id else "unknown"
        return cls(
            provider=provider, model=model_id, policy_version=policy_version or cls.policy_version
        )


@dataclass
class _Entry:
    """Mutable cache entry holding the last good report and refresh bookkeeping."""

    report: AvailabilityReport
    stored_at: datetime
    ttl_seconds: int
    # Refresh coordination: only one in-flight refresh per key at a time.
    refreshing: bool = False
    refresh_error: str | None = None
    last_refresh_attempt: datetime | None = None

    def age_seconds(self, now: datetime) -> float:
        return (now - self.stored_at).total_seconds()

    def is_fresh(self, now: datetime, *, stale_window_seconds: int) -> bool:
        # Freshness is purely TTL-based; the stale window governs revalidation.
        return self.age_seconds(now) <= self.ttl_seconds

    def is_within_stale_window(self, now: datetime, *, stale_window_seconds: int) -> bool:
        age = self.age_seconds(now)
        return self.ttl_seconds < age <= (self.ttl_seconds + stale_window_seconds)


def _default_clock() -> datetime:
    return _now()


def _confidence_for(report: AvailabilityReport) -> float:
    """Deterministic, secret-free confidence in [0, 1] for an explain record.

    Errors collapse confidence to zero. The cache/replay source (``"cache"``)
    is treated as low-trust. Otherwise confidence tracks the eligible share of
    observed candidates.
    """
    if report.errors:
        return 0.0
    if not report.candidates:
        return 0.0 if report.source in ("unknown", "cache") else 0.5
    ratio = len(report.eligible) / len(report.candidates)
    if report.source in ("unknown", "cache"):
        return round(0.3 * ratio, 3)
    return round(ratio, 3)


@dataclass
class AvailabilityCache:
    """Bounded, per-key availability cache with stale-while-revalidate.

    ``get`` returns a cached report when fresh.  When the entry is within
    the stale window it returns the stale report (so callers keep working)
    but triggers a deduplicated refresh for next time.  When the entry is
    past the stale window (or absent) it performs a synchronous refresh and,
    on failure, returns an explicit ``unknown`` report rather than silently
    serving poisoned data.
    """

    source: Callable[[], AvailabilityReport]
    ttl_seconds: int = 60
    stale_window_seconds: int = 30
    policy_version: str = "policy-2026-07-13.1"
    clock: Callable[[], datetime] | None = None
    max_entries: int = 4096
    # Reentrant lock: ``get`` may call ``_refresh_locked`` while already holding
    # the lock (nested acquisition), so a plain ``Lock`` would deadlock.
    _entries: dict[CacheKey, _Entry] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if not isinstance(self.ttl_seconds, int) or self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if not isinstance(self.stale_window_seconds, int) or self.stale_window_seconds < 0:
            raise ValueError("stale_window_seconds must be a non-negative integer")
        if not isinstance(self.max_entries, int) or self.max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        # Reject a non-callable source early; the adapter is required.
        if not callable(self.source):
            raise TypeError("source must be callable and return an AvailabilityReport")

    def _key(self, model_id: str) -> CacheKey:
        return CacheKey.for_candidate(model_id, self.policy_version)

    def get(self, model_id: str) -> AvailabilityReport:
        """Return a cached-or-refreshed report for ``model_id``.

        Fresh -> cached.  Stale-but-within-window -> cached + schedule refresh.
        Expired/missing -> refresh now; on refresh failure return explicit
        ``unknown`` rather than the stale entry.
        """
        key = self._key(model_id)
        now = self.clock() if self.clock else _default_clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.is_fresh(now, stale_window_seconds=self.stale_window_seconds):
                    return entry.report
                if entry.is_within_stale_window(
                    now, stale_window_seconds=self.stale_window_seconds
                ):
                    # Serve stale, but kick a deduplicated refresh.
                    self._maybe_refresh(key, now)
                    return entry.report
            # Expired or missing: must refresh synchronously.
            return self._refresh_locked(key, now)

    def explain(self, model_id: str) -> dict[str, Any]:
        """Return a freshness-aware explain record for ``model_id``.

        Surfaces observed_at, expires_at, age, source, confidence, candidate
        and eligible counts, and the cache/refresh state (``cache_state``,
        ``stale``, ``refreshing``, ``refresh_error``) per the #56 acceptance
        criteria.  Refreshes on first access so the record is never empty.
        """
        key = self._key(model_id)
        now = self.clock() if self.clock else _default_clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.is_fresh(now, stale_window_seconds=self.stale_window_seconds):
                    cache_state = "fresh"
                elif entry.is_within_stale_window(
                    now, stale_window_seconds=self.stale_window_seconds
                ):
                    cache_state = "stale"
                else:
                    cache_state = "expired"
                record = self._explain_entry(entry, cache_state, model_id)
                return record
        # No entry yet: refresh to populate, then explain.
        report = self._refresh_locked(key, now)
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            cache_state = "error" if report.source == "cache" else "expired"
        elif entry.is_fresh(now, stale_window_seconds=self.stale_window_seconds):
            cache_state = "fresh"
        else:
            cache_state = "expired"
        return self._explain_entry(entry, cache_state, model_id, report=report)

    def _explain_entry(
        self,
        entry: _Entry | None,
        cache_state: str,
        model_id: str,
        *,
        report: AvailabilityReport | None = None,
    ) -> dict[str, Any]:
        target = report if report is not None else entry.report if entry is not None else None
        if target is None:
            return {
                "model_id": model_id,
                "policy_version": self.policy_version,
                "cache_state": cache_state,
                "stale": cache_state != "fresh",
                "source": "cache",
                "confidence": 0.0,
                "observed_at": None,
                "expires_at": None,
                "age_seconds": None,
                "freshness_seconds": None,
                "refreshing": False,
                "refresh_error": "availability cache: no entry",
                "errors": ["availability cache: no entry"],
                "candidate_count": 0,
                "eligible_count": 0,
            }
        record = explain_freshness(
            target,
            policy_version=self.policy_version,
            refresh_error=entry.refresh_error if entry is not None else None,
            refreshing=entry.refreshing if entry is not None else False,
        )
        record["model_id"] = model_id
        record["cache_state"] = cache_state
        record["stale"] = cache_state != "fresh"
        return record

    def _maybe_refresh(self, key: CacheKey, now: datetime) -> None:
        """Start a refresh only if none is already in flight (dedup)."""
        entry = self._entries.get(key)
        if entry is not None and entry.refreshing:
            return
        if entry is not None:
            entry.refreshing = True
            entry.last_refresh_attempt = now
        try:
            report = self.source()
            self._store(key, report, now)
        except Exception as exc:
            if entry is not None:
                entry.refresh_error = str(exc)
        finally:
            if entry is not None:
                entry.refreshing = False

    def _refresh_locked(self, key: CacheKey, now: datetime) -> AvailabilityReport:
        """Synchronous refresh; returns report or explicit unknown on failure."""
        # Guard against concurrent refreshes for the same key.
        entry = self._entries.get(key)
        if entry is not None and entry.refreshing:
            # Another thread is refreshing; return stale if present, else unknown.
            if entry.report is not None:
                return entry.report
            return self._unknown_report(str(entry.refresh_error or "refresh in flight"))
        if entry is not None:
            entry.refreshing = True
            entry.last_refresh_attempt = now
        try:
            report = self.source()
        except Exception as exc:
            if entry is not None:
                entry.refresh_error = str(exc)
                entry.refreshing = False
            return self._unknown_report(str(exc))
        finally:
            if entry is not None:
                entry.refreshing = False
        self._store(key, report, now)
        return report

    def _store(self, key: CacheKey, report: AvailabilityReport, now: datetime) -> None:
        if key in self._entries:
            self._entries[key] = replace(
                self._entries[key], report=report, stored_at=now, refresh_error=None
            )
        else:
            if len(self._entries) >= self.max_entries:
                # Evict the oldest stored entry (bounded memory).
                oldest = min(self._entries, key=lambda k: self._entries[k].stored_at)
                del self._entries[oldest]
            self._entries[key] = _Entry(report=report, stored_at=now, ttl_seconds=self.ttl_seconds)

    def _unknown_report(self, detail: str) -> AvailabilityReport:
        return AvailabilityReport((), (), "cache", None, (f"availability cache: {detail}",))

    def keys(self) -> list[str]:
        """Return the model ids currently held in the cache (for diagnostics)."""
        with self._lock:
            return [str(k.model) for k in self._entries]

    def invalidate(self, model_id: str) -> None:
        """Drop a single key (e.g. on explicit policy version change)."""
        with self._lock:
            self._entries.pop(self._key(model_id), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def explain_freshness(
    report: AvailabilityReport,
    *,
    now: datetime | None = None,
    policy_version: str = "policy-2026-07-13.1",
    refresh_error: str | None = None,
    refreshing: bool = False,
) -> dict[str, Any]:
    """Build a freshness-aware explain record from a report.

    Surfaces observed_at, expires_at, age, source, confidence, and any
    refresh/error state so ``/v1/route/explain`` can identify
    freshness and source per the #56 acceptance criteria.
    """
    current = _now(now)
    observed = current
    if report.candidates:
        ages = [c.freshness_seconds for c in report.candidates if c.freshness_seconds is not None]
        if ages:
            observed = current - timedelta(seconds=max(ages))
    expires = observed + timedelta(
        seconds=(report.freshness_seconds if report.freshness_seconds is not None else 0)
    )
    age = (current - observed).total_seconds()
    confidence = _confidence_for(report)
    return {
        "source": report.source,
        "policy_version": policy_version,
        "confidence": confidence,
        "observed_at": observed.isoformat(),
        "expires_at": (expires.isoformat() if report.freshness_seconds is not None else None),
        "age_seconds": round(age, 3),
        "freshness_seconds": report.freshness_seconds,
        "refreshing": refreshing,
        "refresh_error": refresh_error,
        "errors": list(report.errors),
        "candidate_count": len(report.candidates),
        "eligible_count": len(report.eligible),
    }


def build_cache_report(cache: AvailabilityCache, model_ids: list[str]) -> dict[str, Any]:
    """Evaluate the cache for several models and attach a freshness explain block."""
    per_model: dict[str, Any] = {}
    for model_id in model_ids:
        report = cache.get(model_id)
        per_model[model_id] = {
            "report": report,
            "freshness": explain_freshness(report, policy_version=cache.policy_version),
        }
    return per_model
