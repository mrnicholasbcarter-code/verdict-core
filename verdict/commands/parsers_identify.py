"""Native arguments for read-only model identification."""

from typing import Any


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("identify", help="Report passive availability and task ratings")
    parser.add_argument(
        "--passive",
        action="store_true",
        required=True,
        help="Read evidence only; no inference or persistence",
    )
    parser.add_argument("--json", dest="output_json", action="store_true")
