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
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the repo root is importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from verdict.discovery.collect import collect_sources  # noqa: E402
from verdict.discovery.sources._fetcher import Fetcher, default_fetcher  # noqa: E402
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

# Maximum characters logged from an exception message.  Keeps secrets out of
# logs even when they accidentally appear in an exception string.
_MSG_LIMIT = 200


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_msg(exc: BaseException) -> str:
    """Return a truncated, sanitized log message from an exception.

    Logs only the exception type name and the first ``_MSG_LIMIT`` characters
    of ``str(exc)``.  Never calls ``traceback.print_exc`` or equivalent: full
    stack frames can expose auth headers or keys from locals.
    """
    return f"{type(exc).__name__}: {str(exc)[:_MSG_LIMIT]}"


def _remove_stale_cache(cache_dir: Path, name: str) -> None:
    """Remove a source cache file that is now stale due to a fetch failure.

    Uses ``contextlib.suppress`` so a missing file is silently ignored.
    Callers must call this whenever a source fails so readers never see
    old rows as if they were current.
    """
    with contextlib.suppress(OSError):
        (cache_dir / f"{name}.json").unlink()


def _write_cache_atomic(cache_dir: Path, name: str, rows: list[DiscoveryRow]) -> None:
    """Write a source cache file atomically (temp file + os.replace)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.json"
    payload = {
        "schema": "verdict.discovery.sources/v1",
        "source": name,
        "row_count": len(rows),
        "written_at": _iso_now(),
        "rows": [r.to_dict() for r in rows],
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=f".{name}-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    print(f"  wrote {len(rows):>5} rows → {path}", flush=True)


def _write_index_atomic(idx_path: Path, result: dict[str, object]) -> None:
    """Write the _index.json file atomically (temp file + os.replace)."""
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=idx_path.parent, prefix="._index-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, idx_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _make_index_entry(
    source: str,
    *,
    row_count: int,
    error: str | None,
    free_tier_warning: str | None,
    fetched_at: str,
) -> dict[str, Any]:
    """Return a uniform _index.json entry.  All callers use this factory so
    success and failure entries always carry the same set of keys.
    """
    return {
        "error": error,
        "fetched_at": fetched_at,
        "free_tier_warning": free_tier_warning,
        "row_count": row_count,
        "source": source,
    }


def run(
    *,
    sources: list[str] | None = None,
    cache_dir: Path | None = None,
    community: CommunityConfig | None = None,
    omniroute_base_url: str = "http://localhost:20128",
    fetcher: Fetcher = default_fetcher,
    fcm_commit: str | None = None,
    dsh_commit: str | None = None,
) -> dict[str, Any]:
    """Run enabled adapters, cache the results, return an index dict.

    Args:
        sources: Which sources to refresh (default: all).
        cache_dir: Directory for cache files (default: ``$VERDICT_HOME/discovery/sources/``).
        community: Community list pin config.  When ``fcm_commit`` or
            ``dsh_commit`` are also passed they override the corresponding
            field on this object.
        omniroute_base_url: OmniRoute base URL.
        fetcher: HTTP fetcher injected for the network adapters; override in
            tests with :func:`tests.helpers_discovery.make_stub_fetcher`.
        fcm_commit: Override the pinned commit for free-coding-models.
        dsh_commit: Override the pinned commit for dsh-our-free-model.
    """
    enabled = set(sources or _ALL_SOURCES)
    base_cfg = community or CommunityConfig()
    cfg = CommunityConfig(
        fcm_commit=fcm_commit if fcm_commit is not None else base_cfg.fcm_commit,
        dsh_commit=dsh_commit if dsh_commit is not None else base_cfg.dsh_commit,
    )
    home = cache_dir or (verdict_home() / "discovery" / "sources")
    index: list[dict[str, Any]] = []

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
            # Try free-tier summary separately; log but do not abort on failure.
            free_tier_payload = None
            free_tier_warning: str | None = None
            try:
                raw_ft = fetcher(base.rstrip("/") + "/api/free-tier/summary", timeout=20.0)
                free_tier_payload = json.loads(raw_ft)
            except Exception as ft_exc:
                free_tier_warning = _safe_msg(ft_exc)
                print(
                    f"  WARNING [omniroute/free-tier]: {free_tier_warning}",
                    file=sys.stderr,
                    flush=True,
                )
            free_ids = parse_free_tier_ids(free_tier_payload) if free_tier_payload else ()
            or_rows = rows_from_evidence(evidence, base_url=base, free_tier_ids=free_ids)
            index.append(
                _make_index_entry(
                    "omniroute",
                    row_count=len(or_rows),
                    error=None,
                    free_tier_warning=free_tier_warning,
                    fetched_at=_iso_now(),
                )
            )
        except Exception as exc:
            msg = _safe_msg(exc)
            print(f"  ERROR [omniroute]: {msg}", file=sys.stderr, flush=True)
            # Remove stale cache so readers cannot mistake old rows for current data.
            _remove_stale_cache(home, "omniroute")
            index.append(
                _make_index_entry(
                    "omniroute",
                    row_count=0,
                    error=msg,
                    free_tier_warning=None,
                    fetched_at=_iso_now(),
                )
            )
        _write_cache_atomic(home, "omniroute", or_rows)

    # Network adapters: use collect_sources for the remaining sources.
    # collect_sources records per-source errors so we can tell failed sources
    # apart from successfully-empty ones.
    net_result = collect_sources(
        fetcher=fetcher,
        community=cfg,
        include_omniroute=False,
        include_openrouter="openrouter" in enabled,
        include_kilo="kilo" in enabled,
        include_models_dev="models_dev" in enabled,
        include_fcm="free_coding_models" in enabled,
        include_dsh="dsh_our_free_model" in enabled,
    )

    # Build a set of sources that errored so we don't confuse them with empties.
    errored_sources = {e.source: e for e in net_result.errors}
    for err in net_result.errors:
        print(
            f"  ERROR [{err.source}]: {err.error_type}: {err.message}", file=sys.stderr, flush=True
        )

    # Group successful rows by source and cache each separately.
    rows_by_source: dict[str, list[DiscoveryRow]] = {}
    for row in net_result.rows:
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

        if source_key in errored_sources:
            err = errored_sources[source_key]
            # Remove stale cache so a reader cannot mistake old rows for current data.
            _remove_stale_cache(home, file_name)
            index.append(
                _make_index_entry(
                    source_key,
                    row_count=0,
                    error=f"{err.error_type}: {err.message}",
                    free_tier_warning=None,
                    fetched_at=_iso_now(),
                )
            )
        else:
            src_rows = rows_by_source.get(source_key, [])
            index.append(
                _make_index_entry(
                    source_key,
                    row_count=len(src_rows),
                    error=None,
                    free_tier_warning=None,
                    fetched_at=src_rows[0].fetched_at if src_rows else _iso_now(),
                )
            )
            _write_cache_atomic(home, file_name, src_rows)

    result: dict[str, Any] = {
        "schema": "verdict.discovery.sources.index/v1",
        "written_at": _iso_now(),
        "sources": index,
        "total_rows": sum(e.get("row_count", 0) for e in index),
    }
    idx_path = home / "_index.json"
    _write_index_atomic(idx_path, result)
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
    parser.add_argument(
        "--fcm-commit",
        default=None,
        metavar="SHA",
        help="Override the pinned commit SHA for free-coding-models (vava-nessa/free-coding-models).",
    )
    parser.add_argument(
        "--dsh-commit",
        default=None,
        metavar="SHA",
        help="Override the pinned commit SHA for dsh-our-free-model (Ebony-Vinyl/dsh-our-free-model).",
    )
    args = parser.parse_args()
    run(
        sources=args.sources,
        cache_dir=args.cache_dir,
        omniroute_base_url=args.base_url,
        fcm_commit=args.fcm_commit,
        dsh_commit=args.dsh_commit,
    )


if __name__ == "__main__":
    main()
