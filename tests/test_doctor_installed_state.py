"""Doctor's installed-state rows: timers and health-cache freshness (BOD-411 AC1/AC2).

``installed_service_rows``/``health_cache_row`` are exercised directly with a
fake systemd runner and an injectable clock, instead of only patching
unrelated doctor tests so they keep passing.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict import doctor_diagnostics as dd
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def _fake_run(stdout: str):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


@pytest.fixture(autouse=True)
def _isolated_health_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache_path = tmp_path / "health-cache.json"
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(cache_path))
    return cache_path


def _record_one_route(cache_path: Path, *, checked_at: datetime) -> None:
    cache = HealthCache(cache_path)
    cache.record(
        "test/route",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True, http_status=200),
        checked_at,
    )
    cache.save()


# ---------------------------------------------------------------------------
# health_cache_row
# ---------------------------------------------------------------------------


def test_health_cache_row_fails_when_file_missing() -> None:
    row = dd.health_cache_row(now=NOW)
    assert row["status"] == "failed"
    assert "does not exist" in row["detail"]


def test_health_cache_row_fails_when_newest_entry_older_than_2x_probe_interval(
    _isolated_health_cache: Path,
) -> None:
    # Probe interval is 300s; 2x is 600s. 601s old must fail.
    _record_one_route(_isolated_health_cache, checked_at=NOW - timedelta(seconds=601))

    row = dd.health_cache_row(now=NOW)

    assert row["status"] == "failed"
    assert "601" in row["detail"]
    assert "probe interval" in row["detail"]


def test_health_cache_row_ok_when_newest_entry_within_2x_probe_interval(
    _isolated_health_cache: Path,
) -> None:
    _record_one_route(_isolated_health_cache, checked_at=NOW - timedelta(seconds=599))

    row = dd.health_cache_row(now=NOW)

    assert row["status"] == "ok"


def test_health_cache_row_fails_when_fresh_plus_stale_is_zero(_isolated_health_cache: Path) -> None:
    # A negative-category entry is neither fresh nor stale: fresh + stale == 0.
    cache = HealthCache(_isolated_health_cache)
    cache.record(
        "test/route",
        ProbeResult(category="rate_limited", chat_ok=False, tool_ok=False, http_status=429),
        NOW,
    )
    cache.save()

    row = dd.health_cache_row(now=NOW)

    assert row["status"] == "failed"
    assert "no fresh or stale route" in row["detail"]


# ---------------------------------------------------------------------------
# installed_service_rows
# ---------------------------------------------------------------------------


def test_installed_service_rows_fails_when_prove_at_rest_timer_missing(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.prove_at_rest_service import TIMER as PROVE_AT_REST_TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)

    def fake_timer_state(unit: str) -> dict[str, object]:
        if unit == PROVE_AT_REST_TIMER:
            return {"installed": False, "failed": False, "active": False, "state": "missing"}
        return {"installed": True, "failed": False, "active": True, "state": "active"}

    monkeypatch.setattr(dd, "timer_state", fake_timer_state, raising=False)
    import verdict.prove_at_rest_service as pars

    monkeypatch.setattr(pars, "timer_state", fake_timer_state)

    rows = dd.installed_service_rows(now=NOW)

    prove_row = next(r for r in rows if r["label"] == "Prove-at-rest timer")
    assert prove_row["status"] == "failed"
    assert PROVE_AT_REST_TIMER in prove_row["detail"]


def test_installed_service_rows_fails_when_prime_sync_timer_is_failed(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_one_route(_isolated_health_cache, checked_at=NOW)

    def fake_timer_state(unit: str) -> dict[str, object]:
        if unit == "verdict-prime-sync.timer":
            return {"installed": True, "failed": True, "active": False, "state": "failed"}
        return {"installed": True, "failed": False, "active": True, "state": "active"}

    import verdict.prove_at_rest_service as pars

    monkeypatch.setattr(pars, "timer_state", fake_timer_state)

    rows = dd.installed_service_rows(now=NOW)

    sync_row = next(r for r in rows if r["label"] == "Prime-sync timer")
    assert sync_row["status"] == "failed"
    assert "verdict-prime-sync.timer" in sync_row["detail"]


def test_installed_service_rows_all_ok_when_timers_active_and_cache_fresh(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_one_route(_isolated_health_cache, checked_at=NOW)

    import verdict.prove_at_rest_service as pars

    monkeypatch.setattr(
        pars,
        "timer_state",
        lambda unit: {"installed": True, "failed": False, "active": True, "state": "active"},
    )

    rows = dd.installed_service_rows(now=NOW)

    assert all(r["status"] == "ok" for r in rows)
