"""Passive real-traffic evidence in the shared health cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from verdict.admission import evidence_from_at_rest_health_cache
from verdict.orchestration.health_cache import (
    CATEGORY_UPSTREAM,
    EVIDENCE_SOURCE_TRAFFIC,
    STATE_FRESH,
    STATE_NEGATIVE,
    USABLE_SECONDS,
    HealthCache,
    ProbeResult,
)

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
ROUTE = "codestral/codestral-latest"


def _cache(tmp_path: Path) -> HealthCache:
    return HealthCache(tmp_path / "health-cache.json")


def _traffic(cache: HealthCache, observed: datetime) -> str:
    return cache.record_passive_traffic(
        ROUTE, observed_at=observed, now=NOW, source=EVIDENCE_SOURCE_TRAFFIC
    )


def test_traffic_is_chat_only_liveness_aged_by_observation(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    observed = NOW - timedelta(minutes=30)
    assert _traffic(cache, observed) == "recorded"
    entry = cache.entry(ROUTE)
    assert entry is not None
    assert entry.state_at(NOW) == STATE_FRESH
    assert (entry.healthy, entry.chat_ok, entry.tool_ok, entry.agentic_ok) == (
        True,
        True,
        False,
        False,
    )
    assert entry.checked_at == observed
    assert entry.evidence_source == EVIDENCE_SOURCE_TRAFFIC


def test_evidence_source_round_trips_and_probe_entries_omit_it(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.merge_and_save(lambda c: _traffic(c, NOW - timedelta(minutes=1)))
    cache.merge_and_save(
        lambda c: c.record(
            "other/model", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW
        )
    )
    reloaded = _cache(tmp_path)
    assert reloaded.entry(ROUTE).evidence_source == EVIDENCE_SOURCE_TRAFFIC  # type: ignore[union-attr]
    assert reloaded.entry("other/model").evidence_source is None  # type: ignore[union-attr]
    assert "evidence_source" not in reloaded.entry("other/model").to_dict()  # type: ignore[union-attr]


def test_repeated_same_observation_is_not_rewritten(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    observed = NOW - timedelta(minutes=5)
    assert _traffic(cache, observed) == "recorded"
    assert _traffic(cache, observed) == "skipped_newer_or_equal"
    assert _traffic(cache, observed - timedelta(minutes=1)) == "skipped_newer_or_equal"


def test_future_and_too_old_observations_are_not_evidence(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    assert _traffic(cache, NOW + timedelta(minutes=5)) == "skipped_future"
    assert _traffic(cache, NOW - timedelta(seconds=USABLE_SECONDS)) == "skipped_too_old"
    assert cache.entry(ROUTE) is None


def test_unexpired_negative_wins_over_older_and_newer_traffic(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.record(ROUTE, ProbeResult(category=CATEGORY_UPSTREAM, chat_ok=False, tool_ok=False), NOW)
    assert _traffic(cache, NOW - timedelta(minutes=1)) == "skipped_newer_or_equal"
    later = NOW + timedelta(seconds=30)
    assert (
        cache.record_passive_traffic(
            ROUTE, observed_at=later, now=later, source=EVIDENCE_SOURCE_TRAFFIC
        )
        == "skipped_active_negative"
    )
    assert cache.lookup(ROUTE, later).state == STATE_NEGATIVE


def test_traffic_never_replaces_usable_tool_proof(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    probed = NOW - timedelta(hours=2)
    cache.record(ROUTE, ProbeResult(category="ok", chat_ok=True, tool_ok=True), probed)
    assert _traffic(cache, NOW - timedelta(minutes=1)) == "skipped_tool_proof_usable"
    assert cache.entry(ROUTE).tool_ok is True  # type: ignore[union-attr]


def test_traffic_keeps_session_agentic_evidence(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    proof_at = NOW - timedelta(hours=8)
    cache.record_agentic_evidence(ROUTE, passed=True, at=proof_at, source="session_evidence")
    assert _traffic(cache, NOW - timedelta(minutes=1)) == "recorded"
    entry = cache.entry(ROUTE)
    assert entry is not None
    assert (entry.session_agentic_ok, entry.session_agentic_at) == (True, proof_at)
    assert entry.tool_ok is False


def test_traffic_failure_writes_route_negative_aged_by_observation(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    observed = NOW - timedelta(seconds=10)
    outcome = cache.record_passive_failure(
        ROUTE,
        observed_at=observed,
        now=NOW,
        http_status=503,
        category=CATEGORY_UPSTREAM,
        source=EVIDENCE_SOURCE_TRAFFIC,
    )
    assert outcome == "recorded"
    entry = cache.entry(ROUTE)
    assert entry is not None
    assert cache.lookup(ROUTE, NOW).state == STATE_NEGATIVE
    assert (entry.checked_at, entry.failure_scope) == (observed, "route")
    assert entry.evidence_source == EVIDENCE_SOURCE_TRAFFIC


def test_already_expired_failure_leaves_the_entry_untouched(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    assert _traffic(cache, NOW - timedelta(hours=3)) == "recorded"
    before = cache.entry(ROUTE)
    outcome = cache.record_passive_failure(
        ROUTE,
        observed_at=NOW - timedelta(hours=2),
        now=NOW,
        http_status=503,
        category=CATEGORY_UPSTREAM,
        source=EVIDENCE_SOURCE_TRAFFIC,
    )
    assert outcome == "skipped_expired"
    assert cache.entry(ROUTE) == before


def test_passive_liveness_is_never_admission_evidence(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.merge_and_save(lambda c: _traffic(c, NOW - timedelta(minutes=1)))
    evidence = evidence_from_at_rest_health_cache(cache.path, now=NOW)
    assert evidence.observations == ()
