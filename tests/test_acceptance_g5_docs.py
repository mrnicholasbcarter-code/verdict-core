"""
Acceptance tests for G5.1 (THREAT_MODEL.md) and G5.2 (PRIVACY_POLICY.md).

These tests verify:
- Both documentation files exist at the repo root.
- Every file path cited in THREAT_MODEL.md exists in the repo.
- Every file path cited in PRIVACY_POLICY.md exists in the repo.
- THREAT_MODEL.md contains a section for each STRIDE category.

The file-existence checks ensure the docs cannot drift to cite deleted code
without a test failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# File extensions that indicate a citation is a file path.
_FILE_EXTENSIONS = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".sql"}

# Path prefixes that indicate a file inside the repo.
_PATH_PREFIXES = ("verdict/", "tests/", "docs/", "scripts/", "config/", "schemas/", ".github/")


def _looks_like_file_path(candidate: str) -> bool:
    """Return True if *candidate* looks like a relative repo file path."""
    # Must not contain spaces or newlines (those indicate prose, not a path)
    if " " in candidate or "\n" in candidate:
        return False
    # Strip pytest node-id suffix
    file_part = candidate.split("::")[0]
    suffix = Path(file_part).suffix
    return suffix in _FILE_EXTENSIONS or any(file_part.startswith(p) for p in _PATH_PREFIXES)


def _cited_paths(doc_text: str) -> list[str]:
    """Return every backtick-quoted string that looks like a repo file path.

    Matches only single-line backtick spans (no newlines inside).
    Strips pytest ``::test_name`` suffixes before checking existence.
    """
    # Match backtick-quoted spans that contain no newline
    raw = re.findall(r"`([^`\n]+/[^`\n]*)`", doc_text)
    paths: list[str] = []
    for item in raw:
        if not _looks_like_file_path(item):
            continue
        file_part = item.split("::")[0]
        if file_part not in paths:
            paths.append(file_part)
    return paths


def _load_doc(name: str) -> str:
    p = REPO_ROOT / name
    assert p.exists(), f"{name} does not exist at repo root"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Gate G5.1 — THREAT_MODEL.md
# ---------------------------------------------------------------------------


class TestThreatModel:
    def test_file_exists(self) -> None:
        assert (REPO_ROOT / "THREAT_MODEL.md").exists()

    @pytest.mark.parametrize(
        "stride_letter,title",
        [
            ("S", "Spoofing"),
            ("T", "Tampering"),
            ("R", "Repudiation"),
            ("I", "Information"),
            ("D", "Denial"),
            ("E", "Elevation"),
        ],
    )
    def test_stride_category_present(self, stride_letter: str, title: str) -> None:
        text = _load_doc("THREAT_MODEL.md")
        # Accept headings like "## S — Spoofing" or "## Spoofing" etc.
        pattern = re.compile(
            rf"^#+\s+.*(?:{re.escape(stride_letter)}\s*[—\-]\s*{re.escape(title)}|{re.escape(title)})",
            re.MULTILINE | re.IGNORECASE,
        )
        assert pattern.search(text), (
            f"THREAT_MODEL.md is missing a section for STRIDE '{stride_letter}' ({title})"
        )

    def test_cited_paths_exist(self) -> None:
        text = _load_doc("THREAT_MODEL.md")
        paths = _cited_paths(text)
        assert paths, "THREAT_MODEL.md should cite at least one file path"
        missing = [p for p in paths if not (REPO_ROOT / p).exists()]
        assert not missing, (
            "THREAT_MODEL.md cites paths that do not exist in the repo:\n"
            + "\n".join(f"  {p}" for p in missing)
        )


# ---------------------------------------------------------------------------
# Gate G5.2 — PRIVACY_POLICY.md
# ---------------------------------------------------------------------------


class TestPrivacyPolicy:
    def test_file_exists(self) -> None:
        assert (REPO_ROOT / "PRIVACY_POLICY.md").exists()

    @pytest.mark.parametrize(
        "section",
        ["Data processed", "logged", "Retention", "erasure", "Telemetry", "Third part", "Contact"],
    )
    def test_required_section_present(self, section: str) -> None:
        text = _load_doc("PRIVACY_POLICY.md")
        assert re.search(re.escape(section), text, re.IGNORECASE), (
            f"PRIVACY_POLICY.md is missing a section covering '{section}'"
        )

    def test_cited_paths_exist(self) -> None:
        text = _load_doc("PRIVACY_POLICY.md")
        paths = _cited_paths(text)
        assert paths, "PRIVACY_POLICY.md should cite at least one file path"
        missing = [p for p in paths if not (REPO_ROOT / p).exists()]
        assert not missing, (
            "PRIVACY_POLICY.md cites paths that do not exist in the repo:\n"
            + "\n".join(f"  {p}" for p in missing)
        )
