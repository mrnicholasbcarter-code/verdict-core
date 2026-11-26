"""Argument registration for ``verdict deliver``."""

from __future__ import annotations

from typing import Any

EXIT_REFUSED = 3
EXIT_FAILED = 4


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "deliver",
        help="Deliver a verified COMPLETE run as a draft pull request",
        description=(
            "Turn a verified COMPLETE orchestrate run into a draft pull request. "
            f"Exit codes: 0 delivered, reused or dry-run; {EXIT_REFUSED} refused "
            f"(the run is not eligible); {EXIT_FAILED} publish failed."
        ),
    )
    parser.add_argument("run", help="Run id (under --runs-dir) or path to a run directory")
    parser.add_argument(
        "--issue", type=int, required=True, help="GitHub issue number the pull request refers to"
    )
    parser.add_argument(
        "--issue-title", default="", help="Issue title; only used to build the branch slug"
    )
    parser.add_argument(
        "--base", default="main", help="Base branch for the pull request (default: main)"
    )
    parser.add_argument("--runs-dir", default=".verdict/runs", help="Orchestration runs directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned branch, title and body; push and create nothing",
    )
    parser.add_argument("--json", action="store_true", help="Output the outcome as JSON")
