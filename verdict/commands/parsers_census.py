"""Argument registration for the budgeted background census."""

from pathlib import Path
from typing import Any


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser("census", help="Refresh free route passports")
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=768)
    parser.add_argument("--passport-store-path", type=Path)
    parser.add_argument("--health-cache-path", type=Path)
    parser.add_argument("--install", action="store_true", help="Install the 30-minute user timer")
    parser.add_argument("--dry-run", action="store_true", help="Render timer installation only")
    parser.add_argument("--json", dest="output_json", action="store_true")
