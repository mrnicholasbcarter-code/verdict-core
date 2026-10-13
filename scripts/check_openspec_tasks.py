#!/usr/bin/env python3
"""OpenSpec task check: a change touched by this branch has no unticked tasks.

Strict validation (``openspec validate --all --strict``) checks structure. This
check covers lifecycle: every ``openspec/changes/<id>/tasks.md`` that the branch
changed relative to ``--base`` must have every ``- [ ]`` item ticked, unless the
pull request is marked work in progress (``--wip``). A change without
``tasks.md`` fails the same way, because nothing records what was done.

Usage:
    scripts/check_openspec_tasks.py --base origin/main [--wip]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
UNTICKED = re.compile(r"^\s*- \[ \]\s*(.+)$", re.M)


def touched_changes(root: Path, base: str) -> list[str]:
    """Active (not archived) change ids with files changed since ``base``."""
    output = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD", "--", "openspec/changes"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    ids = set()
    for name in output.split():
        parts = PurePosixPath(name).parts
        if len(parts) > 3 and parts[2] != "archive":
            ids.add(parts[2])
    return sorted(change for change in ids if (root / "openspec/changes" / change).is_dir())


def unticked(root: Path, change: str) -> list[str]:
    tasks = root / "openspec/changes" / change / "tasks.md"
    if not tasks.is_file():
        return ["tasks.md is missing"]
    return [match.strip() for match in UNTICKED.findall(tasks.read_text(encoding="utf-8"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", required=True, help="git ref the branch is compared with")
    parser.add_argument("--wip", action="store_true", help="the pull request is work in progress")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    failed = False
    changes = touched_changes(root, args.base)
    for change in changes:
        open_items = unticked(root, change)
        if open_items and not args.wip:
            failed = True
            print(f"openspec change {change}: {len(open_items)} open task(s)")
            for item in open_items:
                print(f"  - [ ] {item}")
    print(f"check_openspec_tasks: {len(changes)} touched change(s){' (wip)' if args.wip else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
