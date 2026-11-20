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

from verdict.discovery.sources._fetcher import default_fetcher  # noqa: E402
from verdict.discovery.sources._types import DiscoveryRow  # noqa: E402
from verdict.discovery.sources.community import (  # noqa: E402
    CommunityConfig,
    fetch_dsh,
    fetch_fcm,
)
from verdict.discovery.sources.kilo import fetch_kilo  # noqa: E402
from verdict.discovery.sources.models_dev import fetch_models_dev  # noqa: E402
from verdict.discovery.sources.omniroute import (  # noqa: E402
    parse_free_tier_ids,
    rows_from_evidence,
)
from verdict.discovery.sources.openrouter import fetch_openrouter  # noqa: E402
from verdict.paths import verdict_home  # noqa: E402

_ALL_SOURCES = (
    "omniroute",
    "openrouter",
    "kilo",
    "models_dev",
    "free_coding_models",
    "dsh_our_free_model",
)


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

    def _run_source(name: str, fn: object, *args: object, **kwargs: object) -> list[DiscoveryRow]:
        try:
            rows = fn(*args, **kwargs)  # type: ignore[call-arg,operator]
            index.append(
                {
                    "source": name,
                    "row_count": len(rows),
                    "error": None,
                    "fetched_at": rows[0].fetched_at if rows else _iso_now(),
                }
            )
            return rows
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            print(f"  ERROR [{name}]: {msg}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            index.append({"source": name, "row_count": 0, "error": msg, "fetched_at": _iso_now()})
            return []

    print(f"Refreshing discovery sources → {home}", flush=True)

    # OmniRoute: requires a live gateway; skip gracefully if unavailable.
    if "omniroute" in enabled:
        or_rows: list[DiscoveryRow] = []
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
            index.append(
                {"source": "omniroute", "row_count": 0, "error": msg, "fetched_at": _iso_now()}
            )
        if or_rows:
            _write_cache(home, "omniroute", or_rows)

    if "openrouter" in enabled:
        rows = _run_source("openrouter", fetch_openrouter, fetcher=default_fetcher)
        if rows:
            _write_cache(home, "openrouter", rows)

    if "kilo" in enabled:
        rows = _run_source("kilo", fetch_kilo, fetcher=default_fetcher)
        if rows:
            _write_cache(home, "kilo", rows)

    if "models_dev" in enabled:
        rows = _run_source("models_dev", fetch_models_dev, fetcher=default_fetcher)
        if rows:
            _write_cache(home, "models_dev", rows)

    if "free_coding_models" in enabled:
        rows = _run_source(
            "free_coding_models", fetch_fcm, fetcher=default_fetcher, commit=cfg.fcm_commit
        )
        if rows:
            _write_cache(home, "free_coding_models", rows)

    if "dsh_our_free_model" in enabled:
        rows = _run_source(
            "dsh_our_free_model", fetch_dsh, fetcher=default_fetcher, commit=cfg.dsh_commit
        )
        if rows:
            _write_cache(home, "dsh_our_free_model", rows)

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
