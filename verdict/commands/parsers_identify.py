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
        "service_command",
        nargs="?",
        choices=["install-service", "uninstall-service", "register-webhook"],
    )
    parser.add_argument(
        "--webhook-url",
        help="Receiver URL OmniRoute posts request events to "
        "(default: http://127.0.0.1:8000 plus the Verdict receiver path)",
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
    parser.add_argument(
        "--refresh-health-cache",
        action="store_true",
        help="With --passive: record observed traffic as chat-only liveness in the health cache",
    )
    parser.add_argument("--allow-live-probe", action="store_true")
    parser.add_argument("--max-per-provider", type=int)
    parser.add_argument("--max-routes", type=int)
    parser.add_argument(
        "--max-capability-tier",
        type=int,
        choices=range(4),
        metavar="{0,1,2,3}",
        help="With --screen: skip, without probing, routes whose capability tier "
        "is unknown or above N (0 = frontier, 3 = small)",
    )
    parser.add_argument(
        "--min-context-tokens",
        type=int,
        metavar="N",
        help="With --screen: skip, without probing, routes whose context window "
        "is unknown or below N tokens",
    )
    parser.add_argument(
        "--require-tools",
        action="store_true",
        help="With --screen: skip, without probing, routes whose tools support is not known",
    )
    parser.add_argument(
        "--max-per-pool",
        type=int,
        metavar="N",
        help="With --screen: probe at most N routes per backend pool (prefixes "
        "sharing one quota count together); pools are covered breadth-first",
    )
    parser.add_argument(
        "--community-roster",
        action="store_true",
        help="Report the current free-coding-models roster; scope a screen to matching routes",
    )
    parser.add_argument(
        "--assume-unmetered-connections",
        action="store_true",
        help="Affirm unmetered connections for a roster screen; ignore list-price-only exclusions",
    )
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
