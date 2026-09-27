"""scripts/check_doc_links.py: broken relative Markdown links fail the docs gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_doc_links", ROOT / "scripts" / "check_doc_links.py"
)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_detects_broken_and_ignores_external_and_fenced(tmp_path: Path) -> None:
    (tmp_path / "ok.md").write_text("x")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[ok](ok.md) [ext](https://x.y) [anchor](#h) [bad](missing.md)\n"
        "```\n[fenced](nope.md)\n```\n"
    )
    assert mod.broken_links(doc, tmp_path) == [(1, "missing.md")]


def test_anchor_matching_heading_resolves(tmp_path: Path) -> None:
    """A link with an in-file anchor is valid only when the target file has a
    heading that slugifies to that anchor (GitHub heading-slug rules)."""
    (tmp_path / "a.md").write_text("# Title\n\n## Section Name\n")
    doc = tmp_path / "doc.md"
    doc.write_text("[a](a.md#section-name)")
    assert mod.broken_links(doc, tmp_path) == []


def test_anchor_not_matching_any_heading_is_broken(tmp_path: Path) -> None:
    """A file that exists but has no heading matching the anchor is a broken link.

    This was previously unchecked: any file#anchor resolved as long as the file
    existed, regardless of whether the anchor pointed at a real heading.
    """
    (tmp_path / "a.md").write_text("# Title\n\nNo matching heading here.\n")
    doc = tmp_path / "doc.md"
    doc.write_text("[a](a.md#section)")
    assert mod.broken_links(doc, tmp_path) == [(1, "a.md#section")]


def test_anchor_slug_keeps_one_hyphen_per_space_like_github(tmp_path: Path) -> None:
    """Punctuation between spaces leaves a double hyphen (github-slugger behaviour)."""
    (tmp_path / "a.md").write_text(
        "# ADR-036 \u2014 Goal to receipt\n\n## G2: Availability & Freshness\n"
    )
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[a](a.md#adr-036--goal-to-receipt)\n"
        "[b](a.md#g2-availability--freshness)\n"
        "[c](a.md#adr-036-goal-to-receipt)\n"
    )
    assert mod.broken_links(doc, tmp_path) == [(3, "a.md#adr-036-goal-to-receipt")]
