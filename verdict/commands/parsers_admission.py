"""Argument registration for the admission snapshot command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "admission", help="Refresh the OmniRoute admission snapshot (no model calls)"
    )
    sub = parser.add_subparsers(dest="admission_command")
    refresh_p = sub.add_parser(
        "refresh",
        help=(
            "Fetch the OmniRoute admission snapshot and write it to ~/.verdict/admit-last-good.json"
        ),
    )
    refresh_p.add_argument(
        "--install-service",
        action="store_true",
        help="Install the 5-minute admit-snapshot keeper timer",
    )
    refresh_p.add_argument(
        "--uninstall", action="store_true", help="Remove the admit-snapshot keeper timer"
    )
    refresh_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Render timer installation only; do not write files or run systemctl",
    )
    refresh_p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output machine-readable JSON result",
    )
