"""Arena snapshot fetcher — stdlib urllib only.

Fetches pages from the Hugging Face datasets-server, filters to 'overall'
category rows with the latest leaderboard_publish_date, joins them against
the store-id index, and writes the resolved snapshot atomically.

The 24-hour gate prevents redundant fetches: if the saved snapshot is less
than 24 hours old for a given config, that config is skipped.  A failed
fetch keeps the previous snapshot and logs the named reason; it never
deletes ratings.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.arena.crosswalk import Crosswalk, load_crosswalk
from verdict.arena.match import build_store_index, match_rows
from verdict.arena.rank import build_reference_sets
from verdict.arena.snapshot import (
    CONFIG_AGENT,
    CONFIG_TEXT,
    CONFIG_WEBDEV,
    ArenaSlot,
    ArenaSnapshot,
    default_snapshot_path,
    load_snapshot,
    save_snapshot,
)

logger = logging.getLogger(__name__)

# Endpoint base (host must be in ALLOWED_METADATA_HOSTS in sources.py)
_HF_DATASETS_SERVER = "https://datasets-server.huggingface.co"
_DATASET = "lmarena-ai/leaderboard-dataset"
_MAX_RETRIES = 3
_RETRY_SLEEP_SECONDS = 2.0
_PAGE_LENGTH = 100
_MAX_PAGES = 50
# 24-hour gate: skip fetch if snapshot is fresher than this
REFRESH_INTERVAL_HOURS = 24

_ARENA_CONFIG_TO_HF = {CONFIG_TEXT: "text", CONFIG_WEBDEV: "webdev", CONFIG_AGENT: "agent"}


def _build_url(path: str, **params: str | int) -> str:
    q = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
    return f"{_HF_DATASETS_SERVER}{path}?{q}"


def _validate_url(url: str) -> str:
    """Validate that the URL scheme and host are allowed before making a request."""
    from verdict.metadata.sources import ALLOWED_METADATA_HOSTS
    from verdict.security import validate_upstream_url

    parts = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    try:
        normalized = validate_upstream_url(origin, allow_private_hosts=set())
    except ValueError as exc:
        raise ValueError(f"arena fetch URL rejected: {exc}") from exc
    host = (urllib.parse.urlsplit(normalized).hostname or "").rstrip(".").lower()
    if host not in ALLOWED_METADATA_HOSTS:
        raise ValueError(f"arena fetch host not allowlisted: {host}")
    return url


def _fetch_json(url: str, sleep_fn: Any = time.sleep) -> Any:
    """Fetch JSON from a URL with retry on 502 / transient errors."""
    _validate_url(url)
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "verdict-metadata-refresh/1"})
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 502 and attempt < _MAX_RETRIES - 1:
                sleep_fn(_RETRY_SLEEP_SECONDS * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < _MAX_RETRIES - 1:
                sleep_fn(_RETRY_SLEEP_SECONDS * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"failed to fetch {url} after {_MAX_RETRIES} attempts")


def _discover_split(hf_config: str, sleep_fn: Any = time.sleep) -> str:
    """Discover the split name for a given HF dataset config."""
    url = _build_url("/splits", dataset=_DATASET, config=hf_config)
    data = _fetch_json(url, sleep_fn)
    splits = [s["split"] for s in data.get("splits", [])]
    if not splits:
        raise ValueError(f"no splits found for config={hf_config!r}")
    # Prefer 'latest', else first
    if "latest" in splits:
        return "latest"
    return str(splits[0])


def _fetch_all_rows(hf_config: str, split: str, sleep_fn: Any = time.sleep) -> list[dict[str, Any]]:
    """Fetch all rows for a config+split, paginated."""
    all_rows: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        url = _build_url(
            "/rows",
            dataset=_DATASET,
            config=hf_config,
            split=split,
            offset=offset,
            length=_PAGE_LENGTH,
        )
        data = _fetch_json(url, sleep_fn)
        rows = data.get("rows", [])
        for item in rows:
            # HF API wraps rows: {"row_idx": N, "row": {...}, "truncated_cells": [...]}
            if isinstance(item, dict) and "row" in item:
                row = dict(item["row"])
            elif isinstance(item, dict):
                row = item
            else:
                continue
            # Inject the config name into the row
            row["_config"] = hf_config
            all_rows.append(row)
        if len(rows) < _PAGE_LENGTH:
            break  # last page
        offset += _PAGE_LENGTH
    return all_rows


def _pick_latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only rows with the latest leaderboard_publish_date and category='overall'."""
    overall = [r for r in rows if r.get("category") == "overall"]
    if not overall:
        return []
    # Pick the latest date
    dates = [r.get("leaderboard_publish_date", "") for r in overall]
    latest = max(dates)
    return [r for r in overall if r.get("leaderboard_publish_date") == latest]


def _is_fresh(
    snapshot: ArenaSnapshot, config_name: str, now: datetime, hours: float = REFRESH_INTERVAL_HOURS
) -> bool:
    """Return True if the snapshot for this config is less than `hours` old."""
    fetched = snapshot.fetched_at.get(config_name)
    if not fetched:
        return False
    try:
        t = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
        if t.tzinfo is None:
            return False
        return (now - t) < timedelta(hours=hours)
    except ValueError:
        return False


def _resolve_records(
    matches: list[Any], config_name: str, publish_date: str
) -> dict[str, dict[str, dict[str, ArenaSlot]]]:
    """Build the records dict fragment for one config from matched rows."""
    out: dict[str, dict[str, dict[str, ArenaSlot]]] = {}
    hf_config = _ARENA_CONFIG_TO_HF.get(config_name, "")
    is_agent = hf_config == "agent"

    for m in matches:
        row = m.row
        effort = m.parsed.effort

        if is_agent:
            raw = row.get("score")
            lo = row.get("score_ci_lower")
            hi = row.get("score_ci_upper")
            n = row.get("session_count")
        else:
            raw = row.get("rating")
            lo = row.get("rating_lower")
            hi = row.get("rating_upper")
            n = row.get("vote_count")

        if raw is None or lo is None or hi is None or n is None:
            continue

        arena_slot = ArenaSlot(
            raw=float(raw),
            lo=float(lo),
            hi=float(hi),
            n=float(n),
            published=publish_date,
            via=m.via,
        )

        for sid in m.store_ids:
            out.setdefault(sid, {})
            out[sid].setdefault(config_name, {})
            out[sid][config_name][effort] = arena_slot

    return out


def build_snapshot_from_rows(
    rows_by_config: dict[str, list[dict[str, Any]]],
    store_ids: frozenset[str],
    crosswalk: Crosswalk,
    fetched_at: dict[str, str],
    now: datetime | None = None,
) -> ArenaSnapshot:
    """Build a full ArenaSnapshot from raw Arena rows."""
    index = build_store_index(store_ids)

    all_records: dict[str, dict[str, dict[str, ArenaSlot]]] = {}
    publish_dates: dict[str, str] = {}
    coverage_summary: dict[str, dict[str, int]] = {}

    for config_name, hf_config in _ARENA_CONFIG_TO_HF.items():
        rows = rows_by_config.get(hf_config, [])
        if not rows:
            continue
        latest_rows = _pick_latest_rows(rows)
        if not latest_rows:
            continue
        publish_date = latest_rows[0].get("leaderboard_publish_date", "")
        publish_dates[config_name] = publish_date

        matches, drops = match_rows(latest_rows, index, crosswalk)
        total = len([r for r in latest_rows if r.get("category") == "overall"])
        coverage_summary[config_name] = {
            "matched": len(matches),
            "dropped": len(drops),
            "total": total,
        }
        fragment = _resolve_records(matches, config_name, publish_date)
        for sid, configs in fragment.items():
            all_records.setdefault(sid, {}).update(configs)

    # Build reference sets per role (populated later by rank module; empty here)
    reference_sets: dict[str, list[str]] = {"planner": [], "implement": [], "reviewer": []}

    return ArenaSnapshot(
        schema_version="1",
        fetched_at=fetched_at,
        leaderboard_publish_date=publish_dates,
        reference_sets=reference_sets,
        records=all_records,
        coverage_summary=coverage_summary,
    )


def refresh_arena_snapshot(
    store_ids: frozenset[str],
    *,
    crosswalk_path: Path | None = None,
    snapshot_path: Path | None = None,
    now: datetime | None = None,
    sleep_fn: Any = time.sleep,
    refresh_hours: float = REFRESH_INTERVAL_HOURS,
) -> dict[str, Any]:
    """Fetch or skip Arena snapshot refresh.

    Returns a status dict with keys: skipped_configs, fetched_configs, error, snapshot_path.
    On failure, keeps the existing snapshot.
    """
    clock = now or datetime.now(timezone.utc)
    snap_path = snapshot_path or default_snapshot_path()
    crosswalk = load_crosswalk(crosswalk_path)

    existing = load_snapshot(snap_path)

    skipped: list[str] = []
    to_fetch: list[str] = []

    for config_name in _ARENA_CONFIG_TO_HF:
        if existing is not None and _is_fresh(existing, config_name, clock, refresh_hours):
            skipped.append(config_name)
            logger.debug("arena: skipping %s (snapshot fresh)", config_name)
        else:
            to_fetch.append(config_name)

    if not to_fetch:
        logger.info("arena: all configs fresh, skipping fetch")
        return {
            "ok": True,
            "skipped_configs": skipped,
            "fetched_configs": [],
            "error": None,
            "snapshot_path": str(snap_path),
        }

    rows_by_config: dict[str, list[dict[str, Any]]] = {}
    fetched_at: dict[str, str] = {}

    # Carry forward existing data for skipped configs
    if existing is not None:
        for config_name in skipped:
            rows_by_config[_ARENA_CONFIG_TO_HF[config_name]] = []  # will be skipped
            fetched_at[config_name] = existing.fetched_at.get(config_name, "")

    try:
        for config_name in to_fetch:
            hf_config = _ARENA_CONFIG_TO_HF[config_name]
            split = _discover_split(hf_config, sleep_fn)
            rows = _fetch_all_rows(hf_config, split, sleep_fn)
            rows_by_config[hf_config] = rows
            ts = clock.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            fetched_at[config_name] = ts
    except Exception as exc:
        logger.warning("arena: fetch failed: %s", exc)
        return {
            "ok": False,
            "skipped_configs": skipped,
            "fetched_configs": [],
            "error": str(exc),
            "snapshot_path": str(snap_path),
        }

    # Build new snapshot, merging with existing skipped-config data
    new_snapshot = build_snapshot_from_rows(rows_by_config, store_ids, crosswalk, fetched_at, clock)

    # Merge existing records for skipped configs
    if existing is not None:
        for config_name in skipped:
            new_snapshot.fetched_at[config_name] = existing.fetched_at.get(config_name, "")
            new_snapshot.leaderboard_publish_date[config_name] = (
                existing.leaderboard_publish_date.get(config_name, "")
            )
            for sid, configs in existing.records.items():
                if config_name in configs:
                    new_snapshot.records.setdefault(sid, {})[config_name] = configs[config_name]

    # Populate reference sets before saving so they're cached in the snapshot.
    build_reference_sets(new_snapshot, clock)

    try:
        save_snapshot(new_snapshot, snap_path)
    except Exception as exc:
        logger.warning("arena: failed to save snapshot: %s", exc)
        return {
            "ok": False,
            "skipped_configs": skipped,
            "fetched_configs": to_fetch,
            "error": f"save failed: {exc}",
            "snapshot_path": str(snap_path),
        }

    return {
        "ok": True,
        "skipped_configs": skipped,
        "fetched_configs": to_fetch,
        "error": None,
        "snapshot_path": str(snap_path),
    }
