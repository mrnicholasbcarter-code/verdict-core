"""Argument registration for the budgeted background census."""

from pathlib import Path
from typing import Any


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("census", help="Refresh free route passports")
    parser.add_argument(
        "--max-candidates", type=int, default=None, help="Optional global probe cap"
    )
    parser.add_argument("--max-tokens", type=int, default=None, help="Optional token budget")
    parser.add_argument(
        "--max-per-provider", type=int, default=2, help="Probe budget per provider family"
    )
    parser.add_argument(
        "--tool-probe-interval-s",
        type=int,
        default=None,
        help="Re-probe proven routes (chat+tool) at least this often; needs VERDICT_PIPELINE_PROBES",
    )
    parser.add_argument(
        "--context-probe-max-per-tick",
        type=int,
        default=1,
        help="Needle context probes per tick; needs VERDICT_PIPELINE_PROBES",
    )
    parser.add_argument("--passport-store-path", type=Path)
    parser.add_argument("--health-cache-path", type=Path)
    parser.add_argument("--install", action="store_true", help="Install the 4-minute user timer")
    parser.add_argument("--dry-run", action="store_true", help="Render timer installation only")
    parser.add_argument("--json", dest="output_json", action="store_true")
