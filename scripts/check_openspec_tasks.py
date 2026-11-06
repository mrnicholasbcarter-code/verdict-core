#!/usr/bin/env python3
"""OpenSpec task check: a change touched by this branch has no unticked tasks.

Strict validation (``openspec validate --all --strict``) checks structure. This
check covers lifecycle: every ``openspec/changes/<id>/tasks.md`` that the branch
changed relative to ``--base`` must have every ``- [ ]`` item ticked, unless the
pull request is marked work in progress (``--wip``). A change without
``tasks.md`` fails the same way, because nothing records what was done.

One kind of unticked task may stay open: a live check that can only run on
merged main, written ``- [ ] (post-merge) <text>``. The line must start, after
any indentation, with exactly ``- [ ] (post-merge) `` and the task text must
not start with whitespace, so a task number goes after the marker. Anything
else, such as ``(post merge)``, ``(postmerge)``, ``(Post-merge)`` or a marker
later in the line, is an ordinary open task. Post-merge tasks are listed but do
not fail this check. ``scripts/check_dod.py`` still counts them: the story is
not done, and its change cannot be archived, until they are ticked.

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
# Applied to a whole UNTICKED match, so it can only reclassify an open task.
POST_MERGE = re.compile(r"\s*- \[ \] \(post-merge\) \S")


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


def unticked(root: Path, change: str) -> tuple[list[str], list[str]]:
    """Unticked items of a change, as (blocking, post-merge)."""
    tasks = root / "openspec/changes" / change / "tasks.md"
    if not tasks.is_file():
        return ["tasks.md is missing"], []
    blocking: list[str] = []
    post_merge: list[str] = []
    for match in UNTICKED.finditer(tasks.read_text(encoding="utf-8")):
        bucket = post_merge if POST_MERGE.match(match.group(0)) else blocking
        bucket.append(match.group(1).strip())
    return blocking, post_merge


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
        open_items, post_merge = unticked(root, change)
        if open_items and not args.wip:
            failed = True
            print(f"openspec change {change}: {len(open_items)} open task(s)")
            for item in open_items:
                print(f"  - [ ] {item}")
        if post_merge:
            print(
                f"openspec change {change}: {len(post_merge)} post-merge task(s)"
                " open until after the merge"
            )
            for item in post_merge:
                print(f"  - [ ] {item}")
    print(f"check_openspec_tasks: {len(changes)} touched change(s){' (wip)' if args.wip else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
