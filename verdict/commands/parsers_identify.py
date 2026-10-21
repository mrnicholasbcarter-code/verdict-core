"""Native arguments for passive identification and bounded consented screening."""

import argparse
from typing import Any


def _capacities(value: str) -> list[str]:
    values = value.split(",")
    if any(item not in {"free", "subscription", "metered", "unknown"} for item in values):
        raise argparse.ArgumentTypeError("capacity must be free, subscription, metered or unknown")
    return values


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("identify", help="Report passive availability and task ratings")
    parser.add_argument(
        "service_command", nargs="?", choices=["install-service", "uninstall-service"]
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--save-report", action="store_true", help="Atomically save passive JSON under Verdict home"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--passive",
        action="store_true",
        help="Read evidence only; no inference (persistence only with --save-report)",
    )
    modes.add_argument("--screen", action="store_true", help="Run bounded missing agentic checks")
    parser.add_argument("--allow-live-probe", action="store_true")
    parser.add_argument("--max-per-provider", type=int)
    parser.add_argument("--max-routes", type=int)
    parser.add_argument("--provider", action="append", default=[], dest="providers")
    parser.add_argument(
        "--capacity", action="extend", type=_capacities, default=[], dest="capacities"
    )
    parser.add_argument(
        "--all-excluded",
        action="store_true",
        help="Include every excluded route (default: first 100, traffic first)",
    )
    parser.add_argument("--json", dest="output_json", action="store_true")
