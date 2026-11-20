"""Arena coverage report.

Runs the join against a snapshot and reports:
- matched / ambiguous (twins) / unmatched counts per config
- top unmatched Arena names
- twin groups (store_id pairs sharing a canonical key)
- |R| per role (reference set size, reflects how many VRs are computable)
- per-config fetched_at and leaderboard_publish_date
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.arena.canonical import slug
from verdict.arena.crosswalk import LintIssue, lint_crosswalk, load_crosswalk
from verdict.arena.match import build_store_index, match_rows
from verdict.arena.rank import _MIN_REF_SET, ROLE_CHAINS, _build_reference_set
from verdict.arena.snapshot import STALENESS_DAYS, ArenaSnapshot


def _twin_groups(store_ids: frozenset[str]) -> list[list[str]]:
    """Return groups of store_ids that share the same canonical (org, slug)."""
    index: dict[tuple[str, str], list[str]] = {}
    for sid in store_ids:
        if "/" not in sid:
            continue
        org, key_part = sid.split("/", 1)
        k = (org, slug(key_part))
        index.setdefault(k, []).append(sid)
    return [sorted(v) for v in index.values() if len(v) > 1]


def coverage_report(
    arena_rows: list[dict[str, Any]],
    store_ids: frozenset[str],
    *,
    crosswalk_path: Path | None = None,
    snapshot: ArenaSnapshot | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generate the full coverage report."""
    clock = now or datetime.now(timezone.utc)
    crosswalk = load_crosswalk(crosswalk_path)
    index = build_store_index(store_ids)

    # Match all rows
    overall_rows = [r for r in arena_rows if r.get("category") == "overall"]
    matches, drops = match_rows(overall_rows, index, crosswalk)

    # Count per config
    configs_seen = {r.get("_config", "") for r in overall_rows}
    matched_by_config: dict[str, int] = {}
    unmatched_by_config: dict[str, int] = {}
    unmatched_names: dict[str, list[str]] = {}

    for r in overall_rows:
        cfg = r.get("_config", "")
        matched_by_config.setdefault(cfg, 0)
        unmatched_by_config.setdefault(cfg, 0)
        unmatched_names.setdefault(cfg, [])

    for m in matches:
        matched_by_config[m._config] = matched_by_config.get(m._config, 0) + 1

    for d in drops:
        cfg_drop = None
        # find config from original rows
        for r in overall_rows:
            if r.get("model_name") == d.model_name and r.get("organization") == d.organization:
                cfg_drop = r.get("_config", "unknown")
                break
        if cfg_drop is None:
            cfg_drop = "unknown"
        unmatched_by_config[cfg_drop] = unmatched_by_config.get(cfg_drop, 0) + 1
        unmatched_names.setdefault(cfg_drop, []).append(d.model_name)

    # Crosswalk lint: missing targets and unused entries
    arena_row_keys: set[tuple[str, str]] = {(m.parsed.org, m.parsed.key) for m in matches}
    lint_issues: list[LintIssue] = lint_crosswalk(crosswalk, arena_row_keys, store_ids)

    # Twin groups
    twins = _twin_groups(store_ids)

    # Reference set sizes per role
    ref_sizes: dict[str, int] = {}
    if snapshot is not None:
        for role_key, chain in ROLE_CHAINS.items():
            for config in chain:
                ref = _build_reference_set(snapshot, config, clock)
                if len(ref) >= _MIN_REF_SET:
                    ref_sizes[role_key] = len(ref)
                    break
            if role_key not in ref_sizes:
                ref_sizes[role_key] = 0

    # Snapshot meta
    snap_meta: dict[str, Any] = {}
    if snapshot is not None:
        snap_meta = {
            "fetched_at": snapshot.fetched_at,
            "leaderboard_publish_date": snapshot.leaderboard_publish_date,
        }

        # Staleness check per config
        stale: dict[str, bool] = {}
        for config_name, pub_date in snapshot.leaderboard_publish_date.items():
            try:
                published = datetime.strptime(pub_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                stale[config_name] = (clock - published).days > STALENESS_DAYS
            except (ValueError, TypeError):
                stale[config_name] = False
        snap_meta["stale"] = stale

    report: dict[str, Any] = {
        "total_overall_rows": len(overall_rows),
        "matched": sum(matched_by_config.values()),
        "unmatched": sum(unmatched_by_config.values()),
        "by_config": {
            cfg: {
                "matched": matched_by_config.get(cfg, 0),
                "unmatched": unmatched_by_config.get(cfg, 0),
                "top_unmatched": unmatched_names.get(cfg, [])[:10],
            }
            for cfg in sorted(configs_seen)
        },
        "twin_groups": twins,
        "twin_group_count": len(twins),
        "reference_set_sizes": ref_sizes,
        "snapshot": snap_meta,
        "lint_issues": [
            {"kind": i.kind, "key": list(i.key), "detail": i.detail} for i in lint_issues
        ],
        "lint_issue_count": len(lint_issues),
    }

    return report


def print_coverage_report(report: dict[str, Any], *, json_output: bool = False) -> None:
    """Print the coverage report to stdout."""
    if json_output:
        print(json.dumps(report, indent=2))
        return

    total = report.get("total_overall_rows", 0)
    matched = report.get("matched", 0)
    unmatched = report.get("unmatched", 0)
    print(f"Arena coverage: {matched}/{total} matched ({unmatched} unmatched)")
    print()

    by_config = report.get("by_config", {})
    for cfg, stats in by_config.items():
        m = stats.get("matched", 0)
        u = stats.get("unmatched", 0)
        print(f"  {cfg}: {m} matched, {u} unmatched")
        top = stats.get("top_unmatched", [])
        if top:
            for name in top[:5]:
                print(f"    - {name!r}")

    twins = report.get("twin_groups", [])
    if twins:
        print(f"\nTwin groups ({len(twins)}):")
        for group in twins[:10]:
            print(f"  {group}")

    ref_sizes = report.get("reference_set_sizes", {})
    if ref_sizes:
        print("\nReference set sizes per role:")
        for role, size in ref_sizes.items():
            print(f"  {role}: |R| = {size}")

    snap = report.get("snapshot", {})
    if snap:
        print("\nSnapshot:")
        for cfg_name, ts in snap.get("fetched_at", {}).items():
            pub = snap.get("leaderboard_publish_date", {}).get(cfg_name, "")
            stale = snap.get("stale", {}).get(cfg_name, False)
            stale_str = " (STALE)" if stale else ""
            print(f"  {cfg_name}: fetched {ts}, published {pub}{stale_str}")
