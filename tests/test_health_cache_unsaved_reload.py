"""reload() refreshes from disk without dropping this object's unsaved records."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from verdict.orchestration.health_cache import HealthCache, ProbeResult

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def test_reload_keeps_an_unsaved_probe_and_picks_up_other_writers(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    mine, other = HealthCache(path), HealthCache(path)
    mine.record("a/route", ProbeResult("ok", True, True), NOW)  # not saved
    other.record("b/route", ProbeResult("ok", True, True), NOW)
    other.save()
    mine.reload()
    assert mine.entry("a/route") is not None and mine.entry("a/route").healthy
    assert mine.entry("b/route") is not None
    assert not path.read_text().count("a/route")  # reload never writes


def test_reload_takes_a_newer_disk_record_over_an_older_unsaved_one(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    mine, other = HealthCache(path), HealthCache(path)
    mine.record("a/route", ProbeResult("ok", True, True), NOW - timedelta(minutes=5))
    other.record("a/route", ProbeResult("timeout", False, False), NOW)
    other.save()
    mine.reload()
    assert mine.entry("a/route") is not None and not mine.entry("a/route").healthy


def test_reload_without_local_changes_mirrors_disk(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    mine, other = HealthCache(path), HealthCache(path)
    other.record("b/route", ProbeResult("ok", True, True), NOW)
    other.save()
    mine.reload()
    assert mine.entry("b/route") is not None
