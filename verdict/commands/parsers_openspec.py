"""OpenSpec lifecycle CLI commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from verdict.openspec_lifecycle import (
    admit_significant_change,
    linear_issue_to_change_id,
    load_change,
)


def register(subparsers: Any) -> None:
    """Register openspec commands with the CLI."""
    openspec = subparsers.add_parser(
        "openspec", help="OpenSpec lifecycle: admission, validation, spec digest"
    )
    openspec_subs = openspec.add_subparsers(dest="openspec_command")

    # verdict openspec admit
    admit_cmd = openspec_subs.add_parser(
        "admit", help="Validate and admit a significant OpenSpec change"
    )
    admit_cmd.add_argument(
        "change_id", help="OpenSpec change ID or Linear issue ID (e.g., bod-205 or BOD-205)"
    )
    admit_cmd.add_argument(
        "--repo", default=".", help="Repository root (default: current directory)"
    )
    admit_cmd.add_argument("--json", action="store_true", help="Output result as JSON")
    admit_cmd.set_defaults(func=_cmd_admit)


def _cmd_admit(args: argparse.Namespace) -> None:
    """Run the admit command."""
    repo = Path(args.repo).resolve()

    # Normalize the change ID (handle Linear issue IDs)
    change_id = args.change_id
    if change_id.upper().startswith("BOD-"):
        change_id = linear_issue_to_change_id(change_id)

    # Load the change
    change = load_change(repo, change_id)

    # Attempt admission
    result = admit_significant_change(change, repo)

    # Output
    if args.json:
        output = {
            "admitted": result.admitted,
            "reason": result.reason,
            "category": result.category,
            "change_id": result.change_id,
        }
        print(json.dumps(output, indent=2))
    else:
        if result.admitted:
            print(f"✓ ADMITTED: {result.change_id}")
            print(f"  {result.reason}")
        else:
            print(f"✗ BLOCKED: {result.change_id or change_id}", file=sys.stderr)
            print(f"  Category: {result.category}", file=sys.stderr)
            print(f"  Reason: {result.reason}", file=sys.stderr)

    # Exit code
    sys.exit(0 if result.admitted else 1)
