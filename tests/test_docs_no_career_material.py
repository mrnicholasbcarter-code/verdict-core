"""Regression test: public docs must not contain career/interview collateral.

This guards against reintroducing career-flavored material (resume drafts,
LinkedIn strategy content, interview story banks, recruiter/hiring-manager
framing) into the public documentation surface, per the public-docs
remediation pass (workstream B).

The verb "resume" (checkpoint/session resume, CLI `resume` command, ADR
"checkpoint_resume" edges) is legitimate orchestration vocabulary and is
deliberately NOT matched here — only the more specific banned terms below
are checked, so `resume`-the-verb is never a false positive.

Two literal git branch-name tokens are allow-listed, and ONLY those exact
tokens: `feat/interview-golden-path` and `cursor/interview-hardening-bod-178-2d40`.
Both are true historical facts (real branch names recorded in a dated audit
log and a fresh-clone certification) rather than career-flavored prose, and
removing them would falsify the historical record. No other occurrence of a
banned term is permitted anywhere in the allow-listed files or elsewhere.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Deliberately does NOT include bare "resume" (a verb used throughout the
# orchestration/checkpoint code and docs, e.g. "resume supervision",
# "--resume <run_id>"). Only career-collateral-specific terms are banned.
BANNED_TERMS = (
    "interview",
    "hiring manager",
    "recruiter",
    "linkedin",
    "story bank",
    "resume suite",
)

# The ONLY two literal strings allowed to contain a banned term anywhere in
# the tracked tree: real git branch names that are historical facts, not
# career-flavored prose. Nothing else is allow-listed.
ALLOWED_TOKENS = ("feat/interview-golden-path", "cursor/interview-hardening-bod-178-2d40")

_TRACKED_ROOTS = ("README.md", "docs", "verdict")


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", *_TRACKED_ROOTS], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def _strip_allowed_tokens(text: str) -> str:
    """Remove the two allow-listed branch-name tokens before scanning."""

    for token in ALLOWED_TOKENS:
        text = text.replace(token, "")
    return text


def _count_hits(text: str, *, strip_allowed: bool = False) -> int:
    scanned = _strip_allowed_tokens(text) if strip_allowed else text
    lowered = scanned.lower()
    return sum(lowered.count(term) for term in BANNED_TERMS)


def test_no_career_material_in_public_docs() -> None:
    violations: dict[str, int] = {}
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits = _count_hits(text, strip_allowed=True)
        if hits:
            rel = path.relative_to(ROOT).as_posix()
            violations[rel] = hits

    assert not violations, (
        f"Career/interview terms found outside the two allow-listed branch-name "
        f"tokens: {violations}. Reword the surrounding prose; do not widen "
        f"ALLOWED_TOKENS."
    )


def test_no_bare_resume_verb_is_matched() -> None:
    """Sanity check: the banned-term list must not match the verb "resume"."""

    sample = "the controller resumes supervision after --resume <run_id>."
    assert _count_hits(sample) == 0


def test_allowed_tokens_are_exact_branch_names_only() -> None:
    """The allow-list must stay narrow: only the two named branch tokens."""

    assert ALLOWED_TOKENS == (
        "feat/interview-golden-path",
        "cursor/interview-hardening-bod-178-2d40",
    )
