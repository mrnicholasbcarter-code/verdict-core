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


def test_is_fresh_handles_non_utc_offset_timestamp() -> None:
    """fromisoformat parses offset-aware timestamps (e.g. +05:30) correctly."""
    # Stored as a non-UTC offset: 1 hour ago in UTC is fresh regardless of the tz label.
    # 2026-11-20T00:30:00+05:30 == 2026-11-19T19:00:00Z, which is 5 h before CLOCK.
    fetched_offset = "2026-11-20T00:30:00+05:30"
    snapshot = _minimal_snapshot(fetched_at={CONFIG_TEXT: fetched_offset})
    assert _is_fresh(snapshot, CONFIG_TEXT, CLOCK, hours=REFRESH_INTERVAL_HOURS) is True


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


# ---- coverage_summary is populated by build_snapshot_from_rows ----------------


def test_build_snapshot_from_rows_has_coverage_summary() -> None:
    """build_snapshot_from_rows must populate coverage_summary with non-zero matched."""
    import json
    from pathlib import Path

    from verdict.arena.crosswalk import load_crosswalk
    from verdict.arena.fetch import build_snapshot_from_rows

    fixtures = Path(__file__).parent / "fixtures"
    text_rows: list[dict] = json.loads((fixtures / "arena-fixture-text.json").read_text())["rows"]
    webdev_rows: list[dict] = json.loads((fixtures / "arena-fixture-webdev.json").read_text())[
        "rows"
    ]
    agent_rows: list[dict] = json.loads((fixtures / "arena-fixture-agent.json").read_text())["rows"]

    cw = load_crosswalk(CW_PATH)
    # Derive store_ids from fixture rows via canonical parse
    from verdict.arena.canonical import Drop, parse_name

    ids: set[str] = set()
    for row in text_rows + webdev_rows + agent_rows:
        parsed = parse_name(row["model_name"], row["organization"], cw.org_providers)
        if isinstance(parsed, Drop):
            continue
        cw_key = (parsed.org, parsed.key)
        if cw_key in cw.map_entries:
            ids.update(cw.map_entries[cw_key].models_dev_ids)
        else:
            for prefix in cw.org_providers.get(parsed.org, [parsed.org]):
                ids.add(f"{prefix}/{parsed.key}")
    store_ids = frozenset(ids)

    rows_by_config = {"text": text_rows, "webdev": webdev_rows, "agent": agent_rows}
    fetched_at = {CONFIG_TEXT: _ts(CLOCK), CONFIG_WEBDEV: _ts(CLOCK), CONFIG_AGENT: _ts(CLOCK)}

    snap = build_snapshot_from_rows(rows_by_config, store_ids, cw, fetched_at, CLOCK)

    assert snap.coverage_summary, "coverage_summary must not be empty"
    total_matched = sum(v.get("matched", 0) for v in snap.coverage_summary.values())
    assert total_matched >= 1, f"expected ≥1 matched row, got {snap.coverage_summary}"

    # Each config entry must have matched + dropped + total
    for cfg, stats in snap.coverage_summary.items():
        assert "matched" in stats, f"{cfg} missing 'matched'"
        assert "dropped" in stats, f"{cfg} missing 'dropped'"
        assert "total" in stats, f"{cfg} missing 'total'"
