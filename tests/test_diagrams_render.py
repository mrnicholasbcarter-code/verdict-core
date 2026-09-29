"""Verify that every file path referenced in docs/diagrams/*.md Mermaid blocks exists."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIAGRAMS_DIR = _REPO_ROOT / "docs" / "diagrams"

# Match verdict/... paths inside Mermaid blocks (parenthesised module references).
_PATH_RE = re.compile(r"\((?P<path>verdict/[a-z0-9_/]+\.py)\)")


def _extract_mermaid_blocks(text: str) -> list[str]:
    """Return the content of every ```mermaid ... ``` block."""
    blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```mermaid"):
            in_block = True
            current = []
        elif in_block and line.strip() == "```":
            blocks.append("\n".join(current))
            in_block = False
        elif in_block:
            current.append(line)
    return blocks


def _referenced_paths(mermaid_text: str) -> set[str]:
    """Extract all verdict/*.py paths from parenthesised annotations."""
    return set(_PATH_RE.findall(mermaid_text))


def _diagram_files() -> list[Path]:
    if not _DIAGRAMS_DIR.is_dir():
        return []
    return sorted(_DIAGRAMS_DIR.glob("*.md"))


@pytest.fixture(params=_diagram_files(), ids=lambda p: p.name)
def diagram_file(request: pytest.FixtureRequest) -> Path:
    return request.param


def test_diagram_files_exist() -> None:
    """At least one diagram file must exist."""
    files = _diagram_files()
    assert files, f"No diagram .md files found in {_DIAGRAMS_DIR}"


def test_mermaid_blocks_present(diagram_file: Path) -> None:
    """Every diagram file must contain at least one mermaid block."""
    text = diagram_file.read_text()
    blocks = _extract_mermaid_blocks(text)
    assert blocks, f"{diagram_file.name} has no ```mermaid blocks"


def test_referenced_paths_exist(diagram_file: Path) -> None:
    """Every verdict/*.py path referenced in a mermaid block must exist on disk."""
    text = diagram_file.read_text()
    blocks = _extract_mermaid_blocks(text)
    all_paths: set[str] = set()
    for block in blocks:
        all_paths |= _referenced_paths(block)
    assert all_paths, f"{diagram_file.name}: no file paths found in mermaid blocks"
    missing = sorted(p for p in all_paths if not (_REPO_ROOT / p).is_file())
    assert not missing, f"{diagram_file.name}: referenced paths not found on disk:\n" + "\n".join(
        f"  {p}" for p in missing
    )
