"""Argument registration for the ``verdict discovery`` command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    discovery_p = subparsers.add_parser("discovery", help="Model-discovery pipeline diagnostics")
    discovery_sub = discovery_p.add_subparsers(dest="discovery_command", required=True)

    funnel_p = discovery_sub.add_parser(
        "funnel",
        help=(
            "Print the model-discovery funnel: listed → free by fact → "
            "probed → answered → tool OK → admitted → considered → selected. "
            "Read-only; makes no model calls, no probes and no writes."
        ),
    )
    funnel_p.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit machine-readable JSON (schema: verdict.discovery.funnel/v1)",
    )

    refresh_p = discovery_sub.add_parser(
        "refresh",
        help=(
            "Probe free and subscription routes at agent size, "
            "record answered/tool-OK results to $VERDICT_HOME/discovery/registry.json."
        ),
    )
    refresh_p.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit machine-readable JSON (schema: verdict.discovery.registry/v1)",
    )
    refresh_p.add_argument(
        "--force",
        action="store_true",
        dest="force",
        help="Re-probe even when a fresh registry already exists (bypass TTL).",
    )
    refresh_p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Zero network calls; returns dry_run_requires_catalog with no entries "
            "(the OmniRoute catalog requires a live fetch). No registry file is written."
        ),
    )
    refresh_p.add_argument(
        "--context-tokens",
        type=int,
        default=18000,
        dest="context_tokens",
        help=(
            "Pad the tool probe to about N tokens (default 18000, the size of "
            "a real agent request). Set 0 to skip padding."
        ),
    )
    refresh_p.add_argument(
        "--wall-budget",
        type=float,
        default=300.0,
        dest="wall_budget_s",
        help="Wall-clock budget in seconds for the full refresh (default 300).",
    )
    refresh_p.add_argument(
        "--free-only",
        action="store_true",
        dest="free_only",
        help=(
            "Probe FREE-capacity routes only; skip subscription routes "
            "(no paid or subscription calls)."
        ),
    )
