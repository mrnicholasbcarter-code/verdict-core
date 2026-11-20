"""Tests for verdict/arena/report.py — coverage_report, _twin_groups, print_coverage_report."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from verdict.arena.canonical import Drop, parse_name
from verdict.arena.crosswalk import load_crosswalk
from verdict.arena.report import _twin_groups, coverage_report, print_coverage_report

WORKTREE = Path(__file__).parent.parent
CW_PATH = WORKTREE / "verdict" / "data" / "arena-crosswalk.json"
FIXTURES = Path(__file__).parent / "fixtures"

# Pinned clock matches fixture publish dates.
CLOCK = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _derive_store_ids(rows: list[dict[str, Any]]) -> frozenset[str]:
    """Build a store_ids frozenset from fixture rows via canonical parse."""
    cw = load_crosswalk(CW_PATH)
    ids: set[str] = set()
    for row in rows:
        parsed = parse_name(row["model_name"], row["organization"], cw.org_providers)
        if isinstance(parsed, Drop):
            continue
        cw_key = (parsed.org, parsed.key)
        if cw_key in cw.map_entries:
            ids.update(cw.map_entries[cw_key].models_dev_ids)
        else:
            for prefix in cw.org_providers.get(parsed.org, [parsed.org]):
                ids.add(f"{prefix}/{parsed.key}")
    return frozenset(ids)


def _load_all_rows() -> list[dict[str, Any]]:
    text_rows: list[dict[str, Any]] = json.loads(
        (FIXTURES / "arena-fixture-text.json").read_text()
    )["rows"]
    webdev_rows: list[dict[str, Any]] = json.loads(
        (FIXTURES / "arena-fixture-webdev.json").read_text()
    )["rows"]
    agent_rows: list[dict[str, Any]] = json.loads(
        (FIXTURES / "arena-fixture-agent.json").read_text()
    )["rows"]
    return text_rows + webdev_rows + agent_rows


# ---- Coverage count test -------------------------------------------------------


def test_coverage_report_matched_at_least_one() -> None:
    """coverage_report must match ≥1 model when given real fixture rows + store_ids."""
    all_rows = _load_all_rows()
    store_ids = _derive_store_ids(all_rows)

    report = coverage_report(all_rows, store_ids, crosswalk_path=CW_PATH, now=CLOCK)

    assert report["matched"] >= 1, "expected at least one matched Arena row"
    assert report["total_overall_rows"] > 0


def test_coverage_report_has_entries_for_each_config() -> None:
    """by_config must contain an entry for each Arena leaderboard config in the rows."""
    all_rows = _load_all_rows()
    store_ids = _derive_store_ids(all_rows)

    report = coverage_report(all_rows, store_ids, crosswalk_path=CW_PATH, now=CLOCK)
    by_config = report["by_config"]

    # Fixture rows carry _config = "text" / "webdev" / "agent"
    assert "text" in by_config, f"by_config keys: {list(by_config.keys())}"
    assert "webdev" in by_config
    assert "agent" in by_config


# ---- Twin group test -----------------------------------------------------------


def test_twin_groups_detects_dot_vs_dash_twins() -> None:
    """store_ids that differ only by dot vs dash slug to the same key → twin group."""
    # Both "anthropic/claude-opus-5" and "anthropic/claude-opus.5" slug to
    # "claude-opus-5", so _twin_groups must return one group containing both.
    store_ids = frozenset({"anthropic/claude-opus-5", "anthropic/claude-opus.5"})
    groups = _twin_groups(store_ids)

    assert len(groups) == 1, f"expected 1 twin group, got {groups}"
    group = groups[0]
    assert "anthropic/claude-opus-5" in group
    assert "anthropic/claude-opus.5" in group


def test_twin_groups_no_twins_for_distinct_keys() -> None:
    """Different base names must not be grouped as twins."""
    store_ids = frozenset({"openai/gpt-4o", "openai/gpt-4-turbo"})
    groups = _twin_groups(store_ids)
    assert groups == []


# ---- print_coverage_report JSON -----------------------------------------------


def test_print_coverage_report_json_is_valid() -> None:
    """print_coverage_report with json_output=True must write parseable JSON."""
    all_rows = _load_all_rows()
    store_ids = _derive_store_ids(all_rows)
    report = coverage_report(all_rows, store_ids, crosswalk_path=CW_PATH, now=CLOCK)

    captured = StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = captured
        print_coverage_report(report, json_output=True)
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    # Must parse as valid JSON without raising
    parsed = json.loads(output)
    assert "matched" in parsed
    assert "by_config" in parsed


# ---- _action_metadata_arena_coverage uses snapshot coverage_summary -----------


def test_action_arena_coverage_reports_nonzero_matched(tmp_path: Path) -> None:
    """_action_metadata_arena_coverage must report non-zero matched when given a real snapshot."""
    from verdict.actions.extra import _action_metadata_arena_coverage
    from verdict.arena.canonical import Drop, parse_name
    from verdict.arena.crosswalk import load_crosswalk
    from verdict.arena.fetch import build_snapshot_from_rows
    from verdict.arena.snapshot import CONFIG_AGENT, CONFIG_TEXT, CONFIG_WEBDEV, save_snapshot

    cw = load_crosswalk(CW_PATH)
    all_rows = _load_all_rows()
    text_rows = [r for r in all_rows if r.get("_config") == "text"]
    webdev_rows = [r for r in all_rows if r.get("_config") == "webdev"]
    agent_rows = [r for r in all_rows if r.get("_config") == "agent"]

    ids: set[str] = set()
    for row in all_rows:
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

    def _ts(dt: datetime) -> str:
        return dt.isoformat().replace("+00:00", "Z")

    rows_by_config = {"text": text_rows, "webdev": webdev_rows, "agent": agent_rows}
    fetched_at = {CONFIG_TEXT: _ts(CLOCK), CONFIG_WEBDEV: _ts(CLOCK), CONFIG_AGENT: _ts(CLOCK)}
    snap = build_snapshot_from_rows(rows_by_config, store_ids, cw, fetched_at, CLOCK)
    snap_path = tmp_path / "arena-snapshot.json"
    save_snapshot(snap, snap_path)

    result = _action_metadata_arena_coverage(snapshot_path=str(snap_path))
    assert result.ok, f"expected ok result, got {result}"
    data = result.data
    assert data.get("matched", 0) >= 1, f"expected ≥1 matched, got {data}"
    assert "by_config" in data
    assert data.get("status") != "no_snapshot"


def test_action_arena_coverage_no_snapshot_named_state(tmp_path: Path) -> None:
    """_action_metadata_arena_coverage must return a named state when no snapshot exists."""
    from verdict.actions.extra import _action_metadata_arena_coverage

    snap_path = tmp_path / "arena-snapshot.json"  # does not exist
    result = _action_metadata_arena_coverage(snapshot_path=str(snap_path))
    assert result.ok
    assert result.data.get("status") == "no_snapshot"
