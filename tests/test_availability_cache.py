"""Tests for the bounded availability cache and issue #56 explain contract.

Covers the acceptance criteria: TTL, stale window, refresh timeout,
invalidation, isolation across providers/policy versions, and the
explain freshness record (observed_at, expires_at, age, source, confidence,
and refresh/error state).  Uses deterministic fake clocks; the source callable
is a controllable fake so refresh timeout/error behavior is exercised directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from llm_gate.availability import AvailabilityReport
from llm_gate.availability_cache import (
    AvailabilityCache,
    CacheKey,
    build_cache_report,
    explain_freshness,
)


def _report(source: str = "fixture", freshness_seconds: float | None = 60) -> AvailabilityReport:
    return AvailabilityReport((), (), source, freshness_seconds, ())


class FakeClock:
    """Deterministic clock the tests advance explicitly."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeSource:
    """Controllable report source that can raise to simulate refresh failure."""

    def __init__(self, report: AvailabilityReport) -> None:
        self.report = report
        self.calls = 0
        self.fail_with: Exception | None = None

    def __call__(self) -> AvailabilityReport:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.report


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def test_cache_key_isolates_provider_and_policy_version() -> None:
    key = CacheKey.for_candidate("openai/gpt-4o")
    assert key.provider == "openai"
    assert key.model == "openai/gpt-4o"
    assert key.policy_version == "policy-2026-07-13.1"

    other = CacheKey.for_candidate("openai/gpt-4o", policy_version="policy-2026-07-14.2")
    assert other != key  # policy change invalidates the entry namespace


def test_source_must_be_callable() -> None:
    with pytest.raises(TypeError):
        AvailabilityCache(source=None)  # type: ignore[arg-type]


def test_ttl_returns_cached_report_without_refresh() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)

    first = cache.get("prov/a")
    second = cache.get("prov/a")
    assert first is second  # same cached object
    assert source.calls == 1  # only the initial synchronous refresh happened


def test_stale_window_serves_stale_then_triggers_single_refresh() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)
    cache.get("prov/a")  # warms the cache (call 1)

    clock.advance(75)  # inside stale window: 60 < age 75 <= 90
    stale = cache.get("prov/a")
    assert stale.source == "fixture"
    # The stale serve scheduled a deduplicated refresh.
    assert source.calls == 2


def test_past_stale_window_refreshes_synchronously() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)
    cache.get("prov/a")
    clock.advance(120)  # past the stale window entirely

    cache.get("prov/a")
    assert source.calls == 2  # re-fetched once, no silent stale serve


def test_refresh_failure_returns_explicit_unknown_not_stale() -> None:
    clock = FakeClock(NOW)
    good = FakeSource(_report())
    cache = AvailabilityCache(source=good, clock=clock, ttl_seconds=60, stale_window_seconds=30)
    cache.get("prov/a")  # warm with a good report
    clock.advance(120)  # expired

    bad = FakeSource(_report())
    bad.fail_with = TimeoutError("upstream timeout")
    cache.source = bad  # type: ignore[assignment]
    report = cache.get("prov/a")
    assert report.source == "cache"
    assert any("availability cache" in e for e in report.errors)


def test_invalidation_drops_entry() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)
    cache.get("prov/a")
    cache.invalidate("prov/a")
    cache.get("prov/a")
    assert source.calls == 2  # invalidation forced a fresh refresh


def test_isolation_across_providers_is_strong() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)
    cache.get("prov/a")
    cache.get("prov-b/x")  # different provider -> distinct key
    assert source.calls == 2
    assert len(cache._entries) == 2


def test_concurrent_get_does_not_deadlock_reentrant_lock() -> None:
    import threading

    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)

    # get() holds the lock and may call _refresh_locked() (nested acquire).
    # A plain Lock would deadlock here; RLock must not.
    results: list[AvailabilityReport] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(cache.get("prov/a"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"deadlock or error: {errors}"
    assert len(results) == 8


def test_explain_freshness_includes_all_acceptance_fields() -> None:
    report = _report(source="omniroute:http", freshness_seconds=60)
    record = explain_freshness(report, now=NOW, refresh_error="boom", refreshing=True)
    assert record["source"] == "omniroute:http"
    assert record["observed_at"] is not None
    assert record["expires_at"] is not None
    assert "age_seconds" in record
    assert "confidence" in record
    assert record["refreshing"] is True
    assert record["refresh_error"] == "boom"
    assert record["policy_version"] == "policy-2026-07-13.1"


def test_cache_explain_exposes_cache_state_and_freshness() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report(source="omniroute:http", freshness_seconds=60))
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)

    fresh = cache.explain("prov/a")
    assert fresh["cache_state"] == "fresh"
    assert fresh["stale"] is False
    # Empty candidate list from a real upstream source -> neutral confidence.
    assert fresh["confidence"] == 0.5

    clock.advance(75)
    stale = cache.explain("prov/a")
    assert stale["cache_state"] == "stale"
    assert stale["stale"] is True


def test_build_cache_report_attaches_freshness() -> None:
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=FakeClock(NOW))
    report = build_cache_report(cache, ["prov/a", "prov/b"])
    assert set(report) == {"prov/a", "prov/b"}
    assert "freshness" in report["prov/a"]


def test_policy_version_change_isolates_entries() -> None:
    clock = FakeClock(NOW)
    source = FakeSource(_report())
    cache = AvailabilityCache(source=source, clock=clock, policy_version="policy-2026-07-13.1")
    cache.get("prov/a")

    other = AvailabilityCache(source=source, clock=clock, policy_version="policy-2026-07-14.2")
    # Different policy_version -> different key namespace in the same source.
    assert other._key("prov/a") != cache._key("prov/a")
