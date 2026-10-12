"""reload() refreshes from disk without dropping this object's unsaved records."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def test_reload_does_not_resurrect_records_this_object_only_read(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    other = HealthCache(path)
    other.record("b/route", ProbeResult("ok", True, True), NOW)
    other.save()
    mine = HealthCache(path)
    mine.record("a/route", ProbeResult("ok", True, True), NOW)  # unsaved local change
    # Another writer replaces the cache wholesale without b/route.
    payload = json.loads(path.read_text())
    payload["routes"] = {}
    path.write_text(json.dumps(payload))
    mine.reload()
    assert mine.entry("b/route") is None
    assert mine.entry("a/route") is not None


def test_failed_save_leaves_changes_unsaved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.orchestration.health_cache as module

    path = tmp_path / "health.json"
    mine = HealthCache(path)
    mine.record("a/route", ProbeResult("ok", True, True), NOW)

    def fail(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(module, "_private_atomic_write", fail)
    with pytest.raises(OSError):
        mine.save()
    monkeypatch.undo()
    mine.reload()
    assert mine.entry("a/route") is not None
