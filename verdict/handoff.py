"""Durable `.verdict/handoff.md` schema for cross-harness resume (cross-harness resume context).

Canonical resumable state lives in Git + worktree + branch + Linear + proof +
this handoff file — never proprietary chat history.

Field vocabulary aligns with Continuity / Prime checkpoint packets
(story/issue, worktree, branch, base_sha, head/current_sha, previous worker).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

HANDOFF_RELATIVE = Path(".verdict") / "handoff.md"
HANDOFF_TITLE = "# Verdict Handoff"

_STORY_ID_RE = re.compile(r"\b([A-Z]{2,10}-\d+)\b", re.IGNORECASE)

_SECTION_ALIASES: dict[str, str] = {
    "completed": "completed",
    "currently working on": "currently_working_on",
    "next exact steps": "next_exact_steps",
    "acceptance criteria": "acceptance_criteria",
    "tests/proof executed": "tests_proof",
    "tests/proof": "tests_proof",
    "files changed": "files_changed",
    "contracts changed": "contracts_changed",
    "important decisions": "important_decisions",
    "known failures": "known_failures",
    "dependencies/blockers": "dependencies_blockers",
    "dependencies": "dependencies_blockers",
    "blockers": "dependencies_blockers",
    "do not / warnings": "do_not",
    "do not": "do_not",
    "warnings": "do_not",
}

_SCALAR_ALIASES: dict[str, str] = {
    "story": "story",
    "worker/harness": "worker",
    "worker": "worker",
    "harness": "worker",
    "worktree": "worktree",
    "branch": "branch",
    "base sha": "base_sha",
    "base_sha": "base_sha",
    "current sha": "current_sha",
    "current_sha": "current_sha",
    "head sha": "current_sha",
    "head_sha": "current_sha",
    "previous worker": "previous_worker",
    "previous_worker": "previous_worker",
    "objective": "objective",
}


class HandoffError(ValueError):
    """Malformed or incomplete handoff document."""


@dataclass
class HandoffDocument:
    """Structured durable handoff for one (or multi-id) story worktree."""

    story: str
    worker: str = ""
    worktree: str = ""
    branch: str = ""
    base_sha: str = ""
    current_sha: str = ""
    previous_worker: str = "none"
    objective: str = ""
    completed: list[str] = field(default_factory=list)
    currently_working_on: list[str] = field(default_factory=list)
    next_exact_steps: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    tests_proof: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    contracts_changed: list[str] = field(default_factory=list)
    important_decisions: list[str] = field(default_factory=list)
    known_failures: list[str] = field(default_factory=list)
    dependencies_blockers: list[str] = field(default_factory=list)
    do_not: list[str] = field(default_factory=list)
    extra_scalars: dict[str, str] = field(default_factory=dict)

    def primary_story_ids(self) -> tuple[str, ...]:
        """Extract normalized story identifiers from the Story field."""
        found = _STORY_ID_RE.findall(self.story or "")
        return tuple(dict.fromkeys(s.upper() for s in found))

    def handoff_path(self) -> Path | None:
        if not self.worktree:
            return None
        return Path(self.worktree) / HANDOFF_RELATIVE


def handoff_path_for(worktree: Path | str) -> Path:
    return Path(worktree) / HANDOFF_RELATIVE


def parse_handoff(text: str) -> HandoffDocument:
    """Parse handoff markdown into a :class:`HandoffDocument`."""
    if not text or not text.strip():
        raise HandoffError("handoff document is empty")

    scalars: dict[str, str] = {}
    sections: dict[str, list[str]] = {v: [] for v in set(_SECTION_ALIASES.values())}
    current_section: str | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == HANDOFF_TITLE or stripped.startswith("# "):
            if stripped.startswith("# ") and stripped != HANDOFF_TITLE:
                # Treat alternate H1 as non-section noise.
                current_section = None
            continue

        section_key = _match_section_header(stripped)
        if section_key is not None:
            current_section = section_key
            continue

        if current_section is None and ":" in stripped:
            key, _, value = stripped.partition(":")
            alias = _SCALAR_ALIASES.get(key.strip().lower())
            if alias:
                scalars[alias] = value.strip()
                continue
            scalars.setdefault("_extra", "")
            # Preserve unknown scalars for forward compatibility.
            if "_extra_map" not in scalars:
                pass
            continue

        if current_section is not None:
            item = _strip_list_marker(stripped)
            if item:
                sections[current_section].append(item)

    # Second pass for unknown scalars collected above — re-scan simply.
    extra: dict[str, str] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.endswith(":"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key_norm = key.strip().lower()
        if key_norm in _SCALAR_ALIASES or _match_section_header(stripped) is not None:
            continue
        if key_norm in {k.lower() for k in _SECTION_ALIASES}:
            continue
        # Skip list-looking lines inside sections (already handled).
        if stripped.startswith(("-", "*", "[")):
            continue
        # Only top-level Key: value before any section — approximate by
        # requiring the key to look like a label (few words, no leading marker).
        if (
            len(key.split()) <= 4
            and value.strip()
            and key_norm not in {a.lower() for a in _SCALAR_ALIASES}
        ):
            extra[key.strip()] = value.strip()

    story = scalars.get("story", "").strip()
    if not story:
        raise HandoffError("handoff requires Story: field")

    return HandoffDocument(
        story=story,
        worker=scalars.get("worker", ""),
        worktree=scalars.get("worktree", ""),
        branch=scalars.get("branch", ""),
        base_sha=scalars.get("base_sha", ""),
        current_sha=scalars.get("current_sha", ""),
        previous_worker=scalars.get("previous_worker", "none") or "none",
        objective=scalars.get("objective", ""),
        completed=sections["completed"],
        currently_working_on=sections["currently_working_on"],
        next_exact_steps=sections["next_exact_steps"],
        acceptance_criteria=sections["acceptance_criteria"],
        tests_proof=sections["tests_proof"],
        files_changed=sections["files_changed"],
        contracts_changed=sections["contracts_changed"],
        important_decisions=sections["important_decisions"],
        known_failures=sections["known_failures"],
        dependencies_blockers=sections["dependencies_blockers"],
        do_not=sections["do_not"],
        extra_scalars=extra,
    )


def read_handoff(path: Path | str) -> HandoffDocument:
    target = Path(path)
    if not target.is_file():
        raise HandoffError(f"handoff not found: {target}")
    return parse_handoff(target.read_text(encoding="utf-8"))


def write_handoff(path: Path | str, doc: HandoffDocument) -> Path:
    """Write handoff markdown atomically (temp file + replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = render_handoff(doc)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    return target


def render_handoff(doc: HandoffDocument) -> str:
    """Serialize a handoff document to the canonical markdown shape."""
    lines: list[str] = [
        HANDOFF_TITLE,
        "",
        f"Story: {doc.story}",
        f"Worker/harness: {doc.worker}",
        f"Worktree: {doc.worktree}",
        f"Branch: {doc.branch}",
        f"Base SHA: {doc.base_sha}",
        f"Current SHA: {doc.current_sha}",
        f"Previous worker: {doc.previous_worker or 'none'}",
        f"Objective: {doc.objective}",
        "",
    ]
    _append_section(lines, "Completed", doc.completed)
    _append_section(lines, "Currently working on", doc.currently_working_on)
    _append_section(lines, "Next exact steps", doc.next_exact_steps, numbered=True)
    _append_section(lines, "Acceptance criteria", doc.acceptance_criteria)
    _append_section(lines, "Tests/proof executed", doc.tests_proof)
    _append_section(lines, "Files changed", doc.files_changed)
    _append_section(lines, "Contracts changed", doc.contracts_changed)
    _append_section(lines, "Important decisions", doc.important_decisions)
    _append_section(lines, "Known failures", doc.known_failures)
    _append_section(lines, "Dependencies/blockers", doc.dependencies_blockers)
    _append_section(lines, "Do not / warnings", doc.do_not)
    return "\n".join(lines).rstrip() + "\n"


def _match_section_header(stripped: str) -> str | None:
    # "Acceptance criteria (cross-harness resume context Wave-1 foundations):"
    if not stripped.endswith(":"):
        return None
    header = stripped[:-1].strip()
    # Drop parenthetical suffixes for matching.
    base = re.sub(r"\s*\([^)]*\)\s*$", "", header).strip().lower()
    return _SECTION_ALIASES.get(base)


def _strip_list_marker(stripped: str) -> str:
    if stripped.startswith(("- ", "* ")):
        return stripped[2:].strip()
    numbered = re.match(r"^\d+\.\s+(.*)$", stripped)
    if numbered:
        return numbered.group(1).strip()
    # Checkbox-only lines under AC keep the marker.
    if stripped.startswith("["):
        return stripped
    return stripped


def _append_section(
    lines: list[str], title: str, items: Sequence[str], *, numbered: bool = False
) -> None:
    lines.append(f"{title}:")
    if not items:
        lines.append("- (none)")
    elif numbered:
        for idx, item in enumerate(items, start=1):
            lines.append(f"{idx}. {item}")
    else:
        for item in items:
            if item.startswith("["):
                lines.append(f"- {item}")
            else:
                lines.append(f"- {item}")
    lines.append("")


def discover_handoff(worktree: Path | str) -> HandoffDocument | None:
    path = handoff_path_for(worktree)
    if not path.is_file():
        return None
    return read_handoff(path)


def iter_story_ids(value: str | Iterable[str]) -> tuple[str, ...]:
    text = value if isinstance(value, str) else " ".join(value)
    return tuple(dict.fromkeys(s.upper() for s in _STORY_ID_RE.findall(text)))
