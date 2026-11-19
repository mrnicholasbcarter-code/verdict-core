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
