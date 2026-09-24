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


def test_anchor_suffix_resolves_file(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x")
    doc = tmp_path / "doc.md"
    doc.write_text("[a](a.md#section)")
    assert mod.broken_links(doc, tmp_path) == []
