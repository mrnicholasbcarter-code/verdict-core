#!/usr/bin/env python3
"""Fail on broken relative links in tracked Markdown files (docs truth gate, BOD-189).

External URLs, anchors-only links and mailto are ignored. Links inside fenced code
blocks are ignored. Exit 1 lists every ``file:line -> target`` that does not resolve.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")
IGNORED_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")


def tracked_markdown(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md", "*.mdx"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return [root / line for line in out.splitlines() if line.strip()]


def broken_links(path: Path, root: Path) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(line):
            target = match.group(1).strip("<>")
            if target.startswith(IGNORED_PREFIXES):
                continue
            file_part = target.split("#", 1)[0]
            if not file_part:
                continue
            resolved = (
                (root / file_part.lstrip("/"))
                if file_part.startswith("/")
                else (path.parent / file_part)
            )
            if not resolved.exists():
                problems.append((number, target))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Markdown files (default: all tracked)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=["docs/archive/"],
        help="Path prefix to skip (default: docs/archive/)",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    files = [Path(p).resolve() for p in args.paths] if args.paths else tracked_markdown(root)
    failures = 0
    for file in files:
        rel = file.relative_to(root).as_posix() if file.is_relative_to(root) else str(file)
        if any(rel.startswith(prefix) for prefix in args.exclude):
            continue
        for number, target in broken_links(file, root):
            print(f"{rel}:{number} -> {target}")
            failures += 1
    if failures:
        print(f"{failures} broken relative link(s)", file=sys.stderr)
        return 1
    print(f"doc links ok ({len(files)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
