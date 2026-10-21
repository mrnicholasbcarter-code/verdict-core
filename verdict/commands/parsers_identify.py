"""Native arguments for passive identification and bounded consented screening."""

from typing import Any


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("identify", help="Report passive availability and task ratings")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--passive", action="store_true", help="Read evidence only; no inference or persistence"
    )
    modes.add_argument("--screen", action="store_true", help="Run bounded missing agentic checks")
    parser.add_argument("--allow-live-probe", action="store_true")
    parser.add_argument("--max-per-provider", type=int)
    parser.add_argument("--max-routes", type=int)
    parser.add_argument("--provider", action="append", default=[], dest="providers")
    parser.add_argument(
        "--capacity",
        action="append",
        default=[],
        choices=["free", "subscription", "metered", "unknown"],
        dest="capacities",
    )
    parser.add_argument("--json", dest="output_json", action="store_true")
