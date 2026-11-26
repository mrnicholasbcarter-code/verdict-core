"""Argument registration for ``verdict deliver``."""

from __future__ import annotations

import argparse
import math
from typing import Any

from verdict.orchestration.deliver_publish import DEFAULT_PUSH_TIMEOUT_S

EXIT_REFUSED = 3
EXIT_FAILED = 4
#: Minutes the ready gate waits for checks by default; ``ready_gate.DEFAULT_CI_DEADLINE_S``
#: is the same value in seconds (pinned by ``tests/test_deliver_ready_wiring.py``).
DEFAULT_CI_DEADLINE_MINUTES = 60.0


def finite_positive_minutes(value: str) -> float:
    """argparse type for ``--ci-deadline``: a finite number of minutes greater than zero.

    ``float`` alone accepts ``nan``, ``inf`` and negatives, which would make the wait
    unbounded (``remaining <= 0`` never holds) or zero.
    """
    try:
        minutes = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of minutes") from None
    if not math.isfinite(minutes) or minutes <= 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a finite number of minutes greater than zero"
        )
    return minutes


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
    parser.add_argument(
        "--ci-deadline",
        type=finite_positive_minutes,
        default=DEFAULT_CI_DEADLINE_MINUTES,
        metavar="MINUTES",
        help=(
            "Minutes (finite, greater than zero) to wait for checks before the ready gate "
            "stops (default: 60)"
        ),
    )
    parser.add_argument(
        "--push-timeout",
        type=finite_positive_minutes,
        default=DEFAULT_PUSH_TIMEOUT_S / 60.0,
        metavar="MINUTES",
        help=(
            "Minutes (finite, greater than zero) allowed for git push, including the "
            "repository's pre-push hook (default: 30)"
        ),
    )
    parser.add_argument(
        "--no-ready",
        action="store_true",
        help="Skip the ready gate; the pull request stays a draft",
    )
    parser.add_argument("--json", action="store_true", help="Output the outcome as JSON")
