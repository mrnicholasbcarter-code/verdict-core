#!/usr/bin/env python3
"""Refresh and cache model-discovery source data.

Runs all discovery source adapters and writes JSON cache files under
``$VERDICT_HOME/discovery/sources/``.  The cache is consumed by story S3's
registry; this script is the production caller that keeps it fresh.

Usage::

    uv run python scripts/refresh_discovery_sources.py [--sources SOURCE...]
    uv run python scripts/refresh_discovery_sources.py --help

Each source writes a separate file:
- ``omniroute.json``
- ``openrouter.json``
- ``kilo.json``
- ``models_dev.json``
- ``free_coding_models.json``
- ``dsh_our_free_model.json``
- ``_index.json`` — summary: source name, row count, fetched_at, errors
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from verdict.discovery.collect import collect_sources  # noqa: E402
from verdict.discovery.sources._fetcher import default_fetcher  # noqa: E402
from verdict.discovery.sources._types import DiscoveryRow  # noqa: E402
from verdict.discovery.sources.community import CommunityConfig  # noqa: E402
from verdict.discovery.sources.omniroute import (  # noqa: E402
    parse_free_tier_ids,
    rows_from_evidence,
)
from verdict.paths import verdict_home  # noqa: E402

_ALL_SOURCES = (
    "omniroute",
    "openrouter",
    "kilo",
    "models_dev",
    "free_coding_models",
    "dsh_our_free_model",
)

# Map from DiscoveryRow.source values to canonical cache file names.
_SOURCE_TO_FILE: dict[str, str] = {
    "omniroute": "omniroute",
    "openrouter": "openrouter",
    "kilo": "kilo",
    "models_dev": "models_dev",
    "free_coding_models": "free_coding_models",
    "dsh_our_free_model": "dsh_our_free_model",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_cache(cache_dir: Path, name: str, rows: list[DiscoveryRow]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.json"
    payload = {
        "schema": "verdict.discovery.sources/v1",
        "source": name,
        "row_count": len(rows),
        "written_at": _iso_now(),
        "rows": [r.to_dict() for r in rows],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"  wrote {len(rows):>5} rows → {path}", flush=True)


def run(
    *,
    sources: list[str] | None = None,
    cache_dir: Path | None = None,
    community: CommunityConfig | None = None,
    omniroute_base_url: str = "http://localhost:20128",
) -> dict[str, object]:
    """Run enabled adapters, cache the results, return an index dict."""
    enabled = set(sources or _ALL_SOURCES)
    cfg = community or CommunityConfig()
    home = cache_dir or (verdict_home() / "discovery" / "sources")
    index: list[dict[str, object]] = []

    print(f"Refreshing discovery sources → {home}", flush=True)

    # OmniRoute: requires a live gateway; handled separately because it needs
    # fetch_omniroute_evidence (not injectable via the Fetcher protocol).
    or_rows: list[DiscoveryRow] = []
    if "omniroute" in enabled:
        try:
            from verdict.omniroute_evidence import fetch_omniroute_evidence

            base = os.environ.get("OMNIROUTE_BASE_URL", omniroute_base_url)
            api_key = os.environ.get("OMNIROUTE_API_KEY") or os.environ.get("LLMGATE_API_KEY")
            mgmt_token = os.environ.get("OMNIROUTE_MANAGEMENT_TOKEN")
            evidence = fetch_omniroute_evidence(base, api_key=api_key, management_token=mgmt_token)
            # Try free-tier summary separately via default_fetcher
            free_tier_payload = None
            try:
                raw_ft = default_fetcher(base.rstrip("/") + "/api/free-tier/summary", timeout=20.0)
                free_tier_payload = json.loads(raw_ft)
            except Exception:
                pass
            free_ids = parse_free_tier_ids(free_tier_payload) if free_tier_payload else ()
            or_rows = rows_from_evidence(evidence, base_url=base, free_tier_ids=free_ids)
            index.append(
                {
                    "source": "omniroute",
                    "row_count": len(or_rows),
                    "error": None,
                    "fetched_at": _iso_now(),
                }
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            print(f"  ERROR [omniroute]: {msg}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            index.append(
                {"source": "omniroute", "row_count": 0, "error": msg, "fetched_at": _iso_now()}
            )
        if or_rows:
            _write_cache(home, "omniroute", or_rows)

    # Network adapters: use collect_sources for the remaining sources.
    # collect_sources swallows per-adapter exceptions, so each is best-effort.
    net_rows = collect_sources(
        fetcher=default_fetcher,
        community=cfg,
        include_omniroute=False,
        include_openrouter="openrouter" in enabled,
        include_kilo="kilo" in enabled,
        include_models_dev="models_dev" in enabled,
        include_community=bool({"free_coding_models", "dsh_our_free_model"} & enabled),
    )

    # Group rows by source and cache each separately, tracking per-source
    # error state from the index (collect_sources swallows exceptions, so
    # we infer success from the row list).
    rows_by_source: dict[str, list[DiscoveryRow]] = {}
    for row in net_rows:
        rows_by_source.setdefault(row.source, []).append(row)

    for source_key, file_name in _SOURCE_TO_FILE.items():
        if source_key == "omniroute":
            continue
        # Only process if the source was requested.
        if source_key == "free_coding_models" and "free_coding_models" not in enabled:
            continue
        if source_key == "dsh_our_free_model" and "dsh_our_free_model" not in enabled:
            continue
        if (
            source_key not in ("free_coding_models", "dsh_our_free_model")
            and source_key not in enabled
        ):
            continue
        src_rows = rows_by_source.get(source_key, [])
        index.append(
            {
                "source": source_key,
                "row_count": len(src_rows),
                "error": None,
                "fetched_at": src_rows[0].fetched_at if src_rows else _iso_now(),
            }
        )
        if src_rows:
            _write_cache(home, file_name, src_rows)

    result: dict[str, object] = {
        "schema": "verdict.discovery.sources.index/v1",
        "written_at": _iso_now(),
        "sources": index,
        "total_rows": sum(e.get("row_count", 0) for e in index),  # type: ignore[call-overload]
    }
    idx_path = home / "_index.json"
    home.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nTotal rows: {result['total_rows']}  Index: {idx_path}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Refresh model-discovery source cache under $VERDICT_HOME/discovery/sources/.")
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=_ALL_SOURCES,
        metavar="SOURCE",
        help=(f"Only refresh these sources (default: all). Choices: {', '.join(_ALL_SOURCES)}"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Override cache directory (default: $VERDICT_HOME/discovery/sources/).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128"),
        help="OmniRoute base URL (default: $OMNIROUTE_BASE_URL or http://localhost:20128).",
    )
    args = parser.parse_args()
    run(sources=args.sources, cache_dir=args.cache_dir, omniroute_base_url=args.base_url)


if __name__ == "__main__":
    main()
