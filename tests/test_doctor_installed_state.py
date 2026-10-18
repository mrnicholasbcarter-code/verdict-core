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


def test_installed_service_rows_reports_metadata_timer_never_completed(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    _record_one_route(_isolated_health_cache, checked_at=NOW)

    from verdict.metadata_service import TIMER as METADATA_TIMER

    states = {
        METADATA_TIMER: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": None,
        }
    }

    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: states.get(
            unit, {"installed": True, "failed": False, "active": True, "state": "active"}
        ),
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert "never completed" in metadata_row["detail"]


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
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    source_names = ("models.dev", "models.dev.models", "litellm")
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=NOW.isoformat().replace("+00:00", "Z"),
            sources={
                source: SourceStatus(
                    source=source,
                    status="ok",
                    fetched_at=NOW.isoformat().replace("+00:00", "Z"),
                    record_count=1,
                )
                for source in source_names
            },
            records=(),
        )
    )

    import verdict.metadata_service as metadata_service
    import verdict.prove_at_rest_service as pars

    def fake_timer(unit: str) -> dict[str, object]:
        del unit
        return {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC",
            "last_completion": "Fri 2026-10-16 11:56:00 UTC",
            "result": "success",
        }

    monkeypatch.setattr(pars, "timer_state", fake_timer)
    monkeypatch.setattr(metadata_service, "timer_state", fake_timer)

    rows = dd.installed_service_rows(now=NOW)

    assert all(r["status"] == "ok" for r in rows)


def test_metadata_timer_absent_is_an_explicit_opt_in_warning(
    _isolated_health_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))

    def timer(unit: str) -> dict[str, object]:
        if unit == TIMER:
            return {"installed": False, "state": "missing", "active": False, "failed": False}
        return {"installed": True, "state": "active", "active": True, "failed": False}

    rows = dd.installed_service_rows(now=NOW, timer_state=timer)
    row = next(item for item in rows if item["label"] == "Metadata refresh timer")
    assert row["status"] == "warning"
    assert "not installed" in row["detail"]
    assert "explicit opt-in" in row["detail"]
    assert not (tmp_path / ".verdict").exists()


def test_metadata_timer_doctor_failure_recovers_after_successful_refresh(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    fetched_at = NOW.isoformat().replace("+00:00", "Z")
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=fetched_at,
            sources={
                source: SourceStatus(
                    source=source, status="ok", fetched_at=fetched_at, record_count=1
                )
                for source in ("models.dev", "models.dev.models", "litellm")
            },
            records=(),
        )
    )
    states = {
        "metadata": {
            "installed": True,
            "failed": True,
            "active": False,
            "state": "failed",
            "last_trigger": None,
            "result": "exit-code",
        }
    }

    def timer(unit: str) -> dict[str, object]:
        if unit == TIMER:
            return states["metadata"]
        return {"installed": True, "failed": False, "active": True, "state": "active"}

    failed = dd.installed_service_rows(now=NOW, timer_state=timer)
    failed_row = next(row for row in failed if row["label"] == "Metadata refresh timer")
    assert failed_row["status"] == "failed"
    assert "never completed" in failed_row["detail"]

    states["metadata"] = {
        "installed": True,
        "failed": False,
        "active": True,
        "state": "active",
        "last_trigger": "Fri 2026-10-16 11:55:00 UTC",
        "result": "success",
    }
    started = dd.installed_service_rows(now=NOW, timer_state=timer)
    started_row = next(row for row in started if row["label"] == "Metadata refresh timer")
    assert started_row["status"] == "failed"
    assert "never completed" in started_row["detail"]

    states["metadata"]["last_completion"] = "Fri 2026-10-16 11:56:00 UTC"
    recovered = dd.installed_service_rows(now=NOW, timer_state=timer)
    recovered_row = next(row for row in recovered if row["label"] == "Metadata refresh timer")
    assert recovered_row["status"] == "ok"
    assert "models.dev=0s" in recovered_row["detail"]


def test_metadata_doctor_reports_stale_source_age(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    fetched_at = (NOW - timedelta(hours=13)).isoformat().replace("+00:00", "Z")
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=fetched_at,
            sources={
                source: SourceStatus(
                    source=source, status="ok", fetched_at=fetched_at, record_count=1
                )
                for source in ("models.dev", "models.dev.models", "litellm")
            },
            records=(),
        )
    )
    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:00:00 UTC" if unit == TIMER else None,
            "result": "success",
        },
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert "older than 12 hours" in metadata_row["detail"]


@pytest.mark.parametrize(
    ("fetched_at", "expected_detail"),
    [
        ("2026-10-16T12:00:00", "unreadable fetch time"),
        ("not-a-time", "unreadable fetch time"),
        ("2026-10-16T13:00:00Z", "fetch time is in the future"),
        (42, "unreadable fetch time"),  # type: ignore[arg-type]
    ],
)
def test_metadata_doctor_fails_closed_on_invalid_source_timestamps(
    _isolated_health_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fetched_at: str,
    expected_detail: str,
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    sources = {
        source: SourceStatus(
            source=source,
            status="ok",
            fetched_at=NOW.isoformat().replace("+00:00", "Z"),
            record_count=1,
        )
        for source in ("models.dev", "models.dev.models", "litellm")
    }
    sources["models.dev"] = SourceStatus(
        source="models.dev", status="ok", fetched_at=fetched_at, record_count=1
    )
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=NOW.isoformat().replace("+00:00", "Z"),
            sources=sources,
            records=(),
        )
    )
    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC" if unit == TIMER else None,
            "result": "success",
        },
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert expected_detail in metadata_row["detail"]


def test_metadata_doctor_keeps_missing_service_result_explicit(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC" if unit == TIMER else None,
        },
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert "last service result is unknown" in metadata_row["detail"]
