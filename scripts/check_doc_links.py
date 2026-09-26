#!/usr/bin/env python3
"""Fail on broken relative links in tracked Markdown files (docs truth gate, BOD-189).

External URLs, anchors-only links and mailto are ignored. Links inside fenced code
blocks are ignored. For links that include an in-file anchor (``path.md#anchor``),
the anchor is verified against the target file's headings using GitHub's heading-slug
rules (lowercase, spaces to hyphens, punctuation other than hyphens stripped;
duplicate headings get a ``-1``, ``-2``, ... suffix). Exit 1 lists every
``file:line -> target`` that does not resolve.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
IGNORED_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")
MARKDOWN_SUFFIXES = (".md", ".mdx")


def tracked_markdown(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md", "*.mdx"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return [root / line for line in out.splitlines() if line.strip()]


def slugify(heading: str) -> str:
    """GitHub heading-slug rules: lowercase, strip punctuation, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def heading_slugs(path: Path) -> set[str]:
    """All valid GitHub anchor slugs for headings in a Markdown file."""
    seen: dict[str, int] = {}
    slugs: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        slug = slugify(match.group(2))
        if not slug:
            continue
        if slug in seen:
            seen[slug] += 1
            slugs.add(f"{slug}-{seen[slug]}")
        else:
            seen[slug] = 0
        slugs.add(slug)
    return slugs


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
            file_part, _, anchor = target.partition("#")
            if not file_part:
                continue
            resolved = (
                (root / file_part.lstrip("/"))
                if file_part.startswith("/")
                else (path.parent / file_part)
            )
            if not resolved.exists():
                problems.append((number, target))
                continue
            if (
                anchor
                and resolved.suffix.lower() in MARKDOWN_SUFFIXES
                and anchor not in heading_slugs(resolved)
            ):
                problems.append((number, target))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Markdown files (default: all tracked)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Path prefix to skip (default: none — every tracked Markdown file is checked)",
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
