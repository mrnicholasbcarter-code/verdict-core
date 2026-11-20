"""Tests for the Arena freshness doctor row (verdict.doctor_diagnostics.arena_snapshot_row).

The function arena_snapshot_row uses VERDICT_HOME to locate the snapshot file.
Tests point VERDICT_HOME at tmp_path to stay isolated from the real ~/.verdict.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_mod = pytest.importorskip("verdict.doctor_diagnostics")
if not hasattr(_mod, "arena_snapshot_row"):
    pytest.skip(
        "verdict.doctor_diagnostics.arena_snapshot_row is not yet implemented",
        allow_module_level=True,
    )

arena_snapshot_row = _mod.arena_snapshot_row  # type: ignore[attr-defined]

from verdict.arena.snapshot import (  # noqa: E402
    CONFIG_AGENT,
    CONFIG_TEXT,
    CONFIG_WEBDEV,
    ArenaSnapshot,
    save_snapshot,
)

# Pinned clock matches fixture publish dates.
CLOCK = datetime(2026, 11, 20, tzinfo=timezone.utc)
_STALENESS_DAYS = 45
_WARNING_HOURS = 48


def _ts(dt: datetime) -> str:
    """Format a datetime as the same Z-suffix form used by the production writer."""
    return dt.isoformat().replace("+00:00", "Z")


def _save_minimal_snapshot(
    *, tmp_path: Path, fetched_hours_ago: float, published_days_ago: int
) -> None:
    """Build and save a minimal snapshot under tmp_path/arena-snapshot.json."""
    fetched_str = _ts(CLOCK - timedelta(hours=fetched_hours_ago))
    publish_str = (CLOCK - timedelta(days=published_days_ago)).strftime("%Y-%m-%d")

    snapshot = ArenaSnapshot(
        schema_version="1",
        fetched_at={
            CONFIG_TEXT: fetched_str,
            CONFIG_WEBDEV: fetched_str,
            CONFIG_AGENT: fetched_str,
        },
        leaderboard_publish_date={
            CONFIG_TEXT: publish_str,
            CONFIG_WEBDEV: publish_str,
            CONFIG_AGENT: publish_str,
        },
        reference_sets={"planner": [], "implement": [], "reviewer": []},
        records={},
    )
    # arena_snapshot_row calls load_snapshot() with no path, which resolves to
    # verdict_home() / "arena-snapshot.json".  VERDICT_HOME is set to tmp_path.
    save_snapshot(snapshot, tmp_path / "arena-snapshot.json")


# ---- Missing snapshot → "missing" ---------------------------------------------


def test_missing_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    # No snapshot saved → file is missing
    row = arena_snapshot_row(now=CLOCK)
    assert row["status"] == "missing"
    assert row["label"] == "Arena ratings"


# ---- All configs fresh → "ok" -------------------------------------------------


def test_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    _save_minimal_snapshot(
        tmp_path=tmp_path,
        fetched_hours_ago=1.0,  # fetched 1h ago (< 48h)
        published_days_ago=1,  # published yesterday (< 45 days)
    )
    row = arena_snapshot_row(now=CLOCK)
    assert row["status"] == "ok"
    assert row["label"] == "Arena ratings"


# ---- Any config fetched ≥ 48 h ago → "warning" --------------------------------


def test_warning_old_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    _save_minimal_snapshot(
        tmp_path=tmp_path,
        fetched_hours_ago=50.0,  # fetched 50h ago (≥ 48h → warning)
        published_days_ago=1,  # published recently (< 45 days, so not stale)
    )
    row = arena_snapshot_row(now=CLOCK)
    # stale check passes (published_days_ago=1 < 45), warning check fires
    assert row["status"] == "warning"


# ---- Any config published > 45 days ago → "stale" -----------------------------


def test_stale_publish_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    _save_minimal_snapshot(
        tmp_path=tmp_path,
        fetched_hours_ago=1.0,  # fetched recently (< 48h, so not a warning trigger)
        published_days_ago=50,  # published 50 days ago (> 45 → stale)
    )
    # stale check fires before warning check
    row = arena_snapshot_row(now=CLOCK)
    assert row["status"] == "stale"


# ---- Staleness boundary: exactly STALENESS_DAYS old is NOT stale ---------------


def test_staleness_boundary_exactly_at_limit_is_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot published exactly STALENESS_DAYS days ago must NOT be stale."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    _save_minimal_snapshot(
        tmp_path=tmp_path,
        fetched_hours_ago=1.0,
        published_days_ago=_STALENESS_DAYS,  # exactly at the boundary
    )
    row = arena_snapshot_row(now=CLOCK)
    assert row["status"] != "stale", (
        f"exactly {_STALENESS_DAYS} days old must not be stale, got {row['status']!r}"
    )


def test_staleness_boundary_one_day_over_limit_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot published STALENESS_DAYS + 1 days ago MUST be stale."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    _save_minimal_snapshot(
        tmp_path=tmp_path, fetched_hours_ago=1.0, published_days_ago=_STALENESS_DAYS + 1
    )
    row = arena_snapshot_row(now=CLOCK)
    assert row["status"] == "stale", f"one day over limit must be stale, got {row['status']!r}"
