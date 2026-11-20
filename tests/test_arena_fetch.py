"""Tests for verdict/arena/fetch.py — _is_fresh, refresh_arena_snapshot (mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from verdict.arena.fetch import REFRESH_INTERVAL_HOURS, _is_fresh, refresh_arena_snapshot
from verdict.arena.snapshot import (
    CONFIG_AGENT,
    CONFIG_TEXT,
    CONFIG_WEBDEV,
    ArenaSlot,
    ArenaSnapshot,
    load_snapshot,
    save_snapshot,
)

WORKTREE = Path(__file__).parent.parent
CW_PATH = WORKTREE / "verdict" / "data" / "arena-crosswalk.json"

# Pinned clock matches fixture publish dates (2026-11-18/19 → well within 45 days).
CLOCK = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _ts(dt: datetime) -> str:
    """Format a datetime as the same Z-suffix form used by the production writer."""
    return dt.isoformat().replace("+00:00", "Z")


def _minimal_snapshot(*, fetched_at: dict[str, str]) -> ArenaSnapshot:
    """Build a minimal snapshot with the given fetched_at mapping."""
    return ArenaSnapshot(
        schema_version="1",
        fetched_at=fetched_at,
        leaderboard_publish_date={
            CONFIG_TEXT: "2026-11-15",
            CONFIG_WEBDEV: "2026-11-15",
            CONFIG_AGENT: "2026-11-15",
        },
        reference_sets={"planner": [], "implement": [], "reviewer": []},
        records={},
    )


def _snapshot_with_one_record(*, fetched_at: dict[str, str]) -> ArenaSnapshot:
    """Minimal snapshot that includes one real model record."""
    slot = ArenaSlot(
        raw=1200.0, lo=1180.0, hi=1220.0, n=10000.0, published="2026-11-15", via="canonical"
    )
    snap = _minimal_snapshot(fetched_at=fetched_at)
    snap.records["openai/gpt-4o"] = {CONFIG_TEXT: {"default": slot}}
    return snap


# ---- _is_fresh tests -----------------------------------------------------------


def test_is_fresh_returns_true_when_fetched_1h_ago() -> None:
    fetched = _ts(CLOCK - timedelta(hours=1))
    snapshot = _minimal_snapshot(fetched_at={CONFIG_TEXT: fetched})
    assert _is_fresh(snapshot, CONFIG_TEXT, CLOCK, hours=REFRESH_INTERVAL_HOURS) is True


def test_is_fresh_returns_false_when_fetched_25h_ago() -> None:
    fetched = _ts(CLOCK - timedelta(hours=25))
    snapshot = _minimal_snapshot(fetched_at={CONFIG_TEXT: fetched})
    assert _is_fresh(snapshot, CONFIG_TEXT, CLOCK, hours=REFRESH_INTERVAL_HOURS) is False


def test_is_fresh_returns_false_when_config_missing() -> None:
    snapshot = _minimal_snapshot(fetched_at={})
    assert _is_fresh(snapshot, CONFIG_TEXT, CLOCK) is False


# ---- refresh_arena_snapshot: skips when all configs are fresh -------------------


def test_refresh_skips_all_when_snapshot_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the snapshot is fresh for all 3 configs, fetch must not be called."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

    fresh = _ts(CLOCK - timedelta(hours=1))
    snap = _minimal_snapshot(
        fetched_at={CONFIG_TEXT: fresh, CONFIG_WEBDEV: fresh, CONFIG_AGENT: fresh}
    )
    snap_path = tmp_path / "arena-snapshot.json"
    save_snapshot(snap, snap_path)

    with patch(
        "verdict.arena.fetch._fetch_json",
        side_effect=AssertionError("_fetch_json must not be called on a fresh snapshot"),
    ) as mock_fetch:
        result = refresh_arena_snapshot(
            store_ids=frozenset(),
            crosswalk_path=CW_PATH,
            snapshot_path=snap_path,
            now=CLOCK,
            sleep_fn=lambda _: None,
        )

    assert result["ok"] is True
    assert result["fetched_configs"] == []
    assert len(result["skipped_configs"]) == 3
    mock_fetch.assert_not_called()


# ---- refresh_arena_snapshot: keeps old snapshot on fetch failure ---------------


def test_refresh_keeps_old_snapshot_on_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fetch must not corrupt or delete the existing snapshot."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

    stale = _ts(CLOCK - timedelta(hours=25))
    old_snap = _snapshot_with_one_record(
        fetched_at={CONFIG_TEXT: stale, CONFIG_WEBDEV: stale, CONFIG_AGENT: stale}
    )
    snap_path = tmp_path / "arena-snapshot.json"
    save_snapshot(old_snap, snap_path)

    original_records = dict(old_snap.records)

    with patch("verdict.arena.fetch._fetch_json", side_effect=OSError("network down")):
        result = refresh_arena_snapshot(
            store_ids=frozenset(),
            crosswalk_path=CW_PATH,
            snapshot_path=snap_path,
            now=CLOCK,
            sleep_fn=lambda _: None,
        )

    assert result["ok"] is False
    assert result["error"] is not None

    # The snapshot on disk must be unchanged
    reloaded = load_snapshot(snap_path)
    assert reloaded is not None, "snapshot file must still exist after fetch failure"
    assert dict(reloaded.records) == original_records, (
        "snapshot records must be unchanged after fetch failure"
    )
