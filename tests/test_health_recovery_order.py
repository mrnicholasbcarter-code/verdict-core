"""Regression coverage for ladder/cache health chronology."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_orch_eligibility import FakeProbe, conn, row
from verdict.orchestration.contracts import EligibilityStage, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import CATEGORY_TIMEOUT, HealthCache, ProbeResult
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
ROUTE = "cc/claude-sonnet-5"
REQ = TaskRequirements()


def _ladder(
    tmp_path: Path,
    probe: FakeProbe | None = None,
    *,
    cache: HealthCache | None = None,
    healthy_ttl_seconds: float = 300.0,
) -> tuple[EligibilityLadder, FakeProbe, HealthCache]:
    probe = probe or FakeProbe()
    cache = cache or HealthCache(tmp_path / "health-cache.json")
    ladder = EligibilityLadder(
        [row(ROUTE, owned_by="claude")],
        [conn("claude")],
        probe,
        tmp_path / "ladder-state.json",
        health_cache=cache,
        healthy_ttl_seconds=healthy_ttl_seconds,
    )
    return ladder, probe, cache


def _cache_failure(cache: HealthCache, *, checked_seconds_ago: float, ttl_seconds: float) -> None:
    cache.record(
        ROUTE,
        ProbeResult(
            CATEGORY_TIMEOUT, chat_ok=False, tool_ok=False, negative_ttl_seconds=ttl_seconds
        ),
        NOW - timedelta(seconds=checked_seconds_ago),
    )


def test_older_ladder_positive_does_not_revive_expired_cache_negative(tmp_path: Path) -> None:
    ladder, probe, cache = _ladder(tmp_path)
    _cache_failure(cache, checked_seconds_ago=120, ttl_seconds=60)

    ladder.record_success(ROUTE, now=NOW - timedelta(seconds=121))

    assert ladder._health_status(ROUTE, NOW) == ("unprobed", CATEGORY_TIMEOUT)
    verdict = ladder.evaluate(REQ, now=NOW)[0]
    assert verdict.failed_stage is None
    assert verdict.reached is EligibilityStage.ENTITLED
    assert verdict.reason == "unprobed"
    assert probe.calls == []


def test_expired_cache_negative_without_newer_success_remains_unprobed(tmp_path: Path) -> None:
    ladder, probe, cache = _ladder(tmp_path)
    _cache_failure(cache, checked_seconds_ago=120, ttl_seconds=60)

    assert ladder._health_status(ROUTE, NOW) == ("unprobed", CATEGORY_TIMEOUT)
    verdict = ladder.evaluate(REQ, now=NOW)[0]
    assert verdict.reason == "unprobed"
    assert probe.calls == []


def test_newer_ladder_probe_success_reused_without_another_probe(tmp_path: Path) -> None:
    ladder, probe, cache = _ladder(
        tmp_path,
        probe=FakeProbe({ROUTE: HealthResult(healthy=False, category="unexpected_reprobe")}),
    )
    _cache_failure(cache, checked_seconds_ago=120, ttl_seconds=60)
    ladder._record_health(
        ROUTE, HealthResult(healthy=True, category="healthy"), NOW - timedelta(seconds=10)
    )

    selected, verdicts = ladder.select(REQ, now=NOW)

    assert selected is not None and selected.route_id == ROUTE
    assert verdicts[0].reason == "selected"
    assert probe.calls == []


def test_stale_newer_ladder_success_is_reprobed(tmp_path: Path) -> None:
    ladder, probe, cache = _ladder(tmp_path, healthy_ttl_seconds=300)
    _cache_failure(cache, checked_seconds_ago=600, ttl_seconds=60)
    ladder.record_success(ROUTE, now=NOW - timedelta(seconds=400))

    assert ladder._health_status(ROUTE, NOW) == ("stale", "")
    selected, _ = ladder.select(REQ, now=NOW)

    assert selected is not None and selected.route_id == ROUTE
    assert probe.calls == [ROUTE]


def test_active_cache_negative_is_never_bypassed_by_newer_ladder_success(tmp_path: Path) -> None:
    ladder, probe, cache = _ladder(tmp_path)
    _cache_failure(cache, checked_seconds_ago=10, ttl_seconds=60)
    ladder.record_success(ROUTE, now=NOW)

    assert ladder._health_status(ROUTE, NOW) == ("unhealthy", CATEGORY_TIMEOUT)
    selected, verdicts = ladder.select(REQ, now=NOW)

    assert selected is None
    assert verdicts[0].failed_stage is EligibilityStage.HEALTHY
    assert verdicts[0].reason == CATEGORY_TIMEOUT
    assert probe.calls == []
