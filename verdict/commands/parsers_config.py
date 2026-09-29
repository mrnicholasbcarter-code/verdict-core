"""Argument registration for Verdict config command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    config_p = subparsers.add_parser("config", help="Inspect Verdict configuration")
    config_sub = config_p.add_subparsers(dest="config_action", required=True)
    show_p = config_sub.add_parser("show", help="Read-only view of current configuration")
    show_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
