"""OpenSpec lifecycle binding for verdict-core.

Maps Linear issues to OpenSpec changes, validates change artifacts, computes
spec revision digests, and enforces admission rules for significant changes.

Authority:
- Linear = queue/intent
- OpenSpec = change contract
- Verdict = plan/execute/verify/receipt
- Git/GitHub = source/PR/merge truth

OpenSpec verification is conformance evidence only. It never marks a story Done
and never replaces Verdict proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Import the vendored validator
from verdict.openspec_vendor.validate_openspec_change import validate_change


@dataclass(frozen=True)
class AdmissionResult:
    """Result of attempting to admit a significant change."""

    admitted: bool
    """True if the change passed all admission checks."""

    reason: str
    """Human-readable explanation of the result."""

    change_id: str | None = None
    """OpenSpec change ID if found."""

    category: str = "ok"
    """Category: 'ok', 'blocked', 'openspec_unavailable'."""


@dataclass(frozen=True)
class OpenSpecChange:
    """Structured OpenSpec change state."""

    change_id: str
    """OpenSpec change identifier."""

    linear_issue: str | None
    """Associated Linear issue ID (if reverse-mapped)."""

    schema: str
    """OpenSpec schema name (e.g., 'verdict-change-v1')."""

    change_dir: Path
    """Path to the change directory."""

    artifacts: dict[str, Any]
    """Raw artifact content keyed by filename."""


def _is_placeholder_only(text: str) -> bool:
    """Check if text contains only placeholder content.

    A body is placeholder-only if every non-empty line, after stripping bullet
    markers (-, *, •, digits+'.'), whitespace and trailing punctuation (.:;!),
    is in {tbd, todo, n/a, na, none, -, ..., …, tba, pending} (case-insensitive)
    or is empty.
    """
    if not text.strip():
        return True

    # Placeholder values (case-insensitive)
    placeholders = {"tbd", "todo", "n/a", "na", "none", "-", "...", "…", "tba", "pending"}

    lines = text.split("\n")
    for line in lines:
        # Strip whitespace
        stripped = line.strip()
        if not stripped:
            continue  # Empty lines are OK

        # Check if the line itself (before bullet stripping) is a bare placeholder
        if stripped.lower() in placeholders:
            continue

        # Strip bullet markers: -, *, •, or digit(s) followed by '.'
        # Remove leading bullets
        stripped = re.sub(r"^[-*•]\s*", "", stripped)
        # Remove numbered list markers (e.g., "1. ", "12. ")
        stripped = re.sub(r"^\d+\.\s*", "", stripped)

        # Strip trailing punctuation
        stripped = stripped.rstrip(".:;!")

        # Check if what remains is a placeholder
        if stripped and stripped.lower() not in placeholders:
            return False  # Found a real line

    return True  # All lines were placeholders or empty


def _extract_section_body(content: str, heading: str) -> str | None:
    """Extract the body of a markdown section by heading."""
    lines = content.split("\n")
    in_section = False
    body_lines = []

    # Normalize the heading for comparison
    heading_lower = heading.lower().lstrip("#").strip()

    for line in lines:
        # Check if this is a heading line
        if line.startswith("#"):
            current_heading = line.lstrip("#").strip().lower()

            if current_heading == heading_lower:
                in_section = True
                continue
            elif in_section:
                # We've hit another heading, stop
                break
        elif in_section:
            body_lines.append(line)

    if not body_lines:
        return None

    return "\n".join(body_lines).strip()


def _check_placeholder_sections(change_dir: Path) -> list[str]:
    """Check for placeholder-only content in required sections.

    Returns a list of error messages for any required sections that contain
    only placeholder content.

    Checks:
    - proposal.md: Why, What Changes, Impact
    - design.md: Context, Goals / Non-Goals, Decisions
    - specs/**/spec.md: each Requirement body
    - tasks.md: at least one real task checkbox
    """
    errors = []

    # Required sections in proposal.md and design.md
    required_substantive = [
        ("proposal.md", "## Why"),
        ("proposal.md", "## What Changes"),
        ("proposal.md", "## Impact"),
        ("design.md", "## Context"),
        ("design.md", "## Goals / Non-Goals"),
        ("design.md", "## Decisions"),
    ]

    for filename, heading in required_substantive:
        filepath = change_dir / filename
        if not filepath.exists():
            continue  # Structural validation will catch missing files

        content = filepath.read_text(encoding="utf-8")
        body = _extract_section_body(content, heading)

        if body is None:
            continue  # Structural validation will catch missing headings

        if _is_placeholder_only(body):
            errors.append(f"{filename}: section '{heading}' contains only placeholder content")

    # Check spec requirements
    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        for spec_file in specs_dir.rglob("*.md"):
            content = spec_file.read_text(encoding="utf-8")
            rel_path = str(spec_file.relative_to(change_dir))

            # Extract each requirement body
            lines = content.split("\n")
            in_requirement = False
            requirement_name = ""
            requirement_body_lines: list[str] = []

            for line in lines:
                if line.startswith("### Requirement:"):
                    # Process previous requirement
                    if in_requirement and requirement_body_lines:
                        body = "\n".join(requirement_body_lines).strip()
                        if _is_placeholder_only(body):
                            errors.append(
                                f"{rel_path}: requirement '{requirement_name}' has only placeholder content"
                            )

                    # Start new requirement
                    in_requirement = True
                    requirement_name = line.replace("### Requirement:", "").strip()
                    requirement_body_lines = []
                elif line.startswith("###") and not line.startswith("####"):
                    # End of requirements section
                    if in_requirement and requirement_body_lines:
                        body = "\n".join(requirement_body_lines).strip()
                        if _is_placeholder_only(body):
                            errors.append(
                                f"{rel_path}: requirement '{requirement_name}' has only placeholder content"
                            )
                    in_requirement = False
                elif in_requirement:
                    requirement_body_lines.append(line)

            # Check last requirement
            if in_requirement and requirement_body_lines:
                body = "\n".join(requirement_body_lines).strip()
                if _is_placeholder_only(body):
                    errors.append(
                        f"{rel_path}: requirement '{requirement_name}' has only placeholder content"
                    )

    # Check tasks.md for at least one real task
    tasks_file = change_dir / "tasks.md"
    if tasks_file.exists():
        content = tasks_file.read_text(encoding="utf-8")
        # Look for task checkboxes: - [ ] or - [x]
        task_pattern = re.compile(r"^\s*-\s*\[[ xX]\]\s*\d+\.\d+\s+(.+)$", re.MULTILINE)
        tasks = task_pattern.findall(content)

        if not tasks:
            errors.append("tasks.md: no task checkboxes found (- [ ] X.Y ...)")
        else:
            # Check if all tasks are placeholder-only
            all_placeholder = all(_is_placeholder_only(task) for task in tasks)
            if all_placeholder:
                errors.append("tasks.md: all tasks contain only placeholder content")

    return errors


def linear_issue_to_change_id(issue_id: str) -> str:
    """Map a Linear issue ID to an OpenSpec change ID.

    Rule: BOD-205 -> bod-205 (lowercase, stable).
    The full change ID includes a slug (e.g., bod-205-openspec-lifecycle),
    but this returns the stable prefix for lookup.

    Args:
        issue_id: Linear issue identifier (e.g., "BOD-205")

    Returns:
        OpenSpec change ID prefix (e.g., "bod-205")

    Example:
        >>> linear_issue_to_change_id("BOD-205")
        'bod-205'
    """
    return issue_id.lower()


def change_id_to_linear_issue(change_id: str) -> str | None:
    """Reverse lookup: OpenSpec change ID to Linear issue ID.

    Args:
        change_id: OpenSpec change identifier (e.g., "bod-205-openspec-lifecycle")

    Returns:
        Linear issue ID if pattern matches, None otherwise.

    Example:
        >>> change_id_to_linear_issue("bod-205-openspec-lifecycle")
        'BOD-205'
    """
    # Extract BOD-NNN pattern from the start of the change ID
    match = re.match(r"^(bod-\d+)", change_id, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None


def _run_openspec_command(
    args: list[str], cwd: Path | None = None, check: bool = False, timeout: float = 60.0
) -> subprocess.CompletedProcess[str] | None:
    """Run an openspec command with OPENSPEC_TELEMETRY=0.

    Args:
        args: Command arguments (without npx/openspec prefix)
        cwd: Working directory
        check: Raise on non-zero exit
        timeout: Timeout in seconds

    Returns:
        CompletedProcess if successful, None if unavailable (subprocess error)
    """
    import os

    env = os.environ.copy()
    env["OPENSPEC_TELEMETRY"] = "0"

    try:
        return subprocess.run(
            ["npx", "-y", "@fission-ai/openspec@1.13.2", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            check=check,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        # npx not found, execution failed, or timeout
        return None


def load_change(repo: Path, change_id: str) -> OpenSpecChange | None:
    """Load an OpenSpec change from the repository.

    Prefers structured `openspec change show --json` output. Falls back to
    reading files directly only for fields without structured output.

    Args:
        repo: Repository root path
        change_id: OpenSpec change identifier

    Returns:
        OpenSpecChange if found, None if the change doesn't exist or openspec is unavailable.
    """
    # First, try to get structured data from openspec CLI
    result = _run_openspec_command(
        ["change", "show", change_id, "--json", "--no-interactive"], cwd=repo
    )

    if result is None or result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    # Get change directory path
    change_dir = repo / "openspec" / "changes" / change_id
    if not change_dir.exists():
        return None

    # Read artifacts for fields not in JSON
    artifacts = {}
    for artifact_file in ["proposal.md", "design.md", "tasks.md"]:
        artifact_path = change_dir / artifact_file
        if artifact_path.exists():
            artifacts[artifact_file] = artifact_path.read_text(encoding="utf-8")

    # Read spec files
    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        for spec_file in specs_dir.rglob("*.md"):
            rel_path = str(spec_file.relative_to(change_dir))
            artifacts[rel_path] = spec_file.read_text(encoding="utf-8")

    # Get schema from .openspec.yaml or data
    schema = data.get("schema", "verdict-change-v1")

    return OpenSpecChange(
        change_id=change_id,
        linear_issue=change_id_to_linear_issue(change_id),
        schema=schema,
        change_dir=change_dir,
        artifacts=artifacts,
    )


def admit_significant_change(change: OpenSpecChange | None, repo: Path) -> AdmissionResult:
    """Admit a significant change after validation.

    FAILS CLOSED with explicit reason when:
    - Change is missing
    - Required verdict-change-v1 artifacts are missing or invalid
    - Any required section body is a PLACEHOLDER only
    - OpenSpec is unavailable

    Args:
        change: OpenSpec change to validate (None = not found)
        repo: Repository root for running vendored validator

    Returns:
        AdmissionResult indicating admission status and reason.
    """
    if change is None:
        # Check if openspec CLI is available
        check_result = _run_openspec_command(["--version"])
        if check_result is None:
            # _run_openspec_command returned None due to subprocess error
            # The specific exception (FileNotFoundError, OSError, TimeoutExpired) was caught internally
            return AdmissionResult(
                admitted=False,
                reason="OpenSpec CLI unavailable (subprocess error: FileNotFoundError, OSError, or TimeoutExpired)",
                category="openspec_unavailable",
            )

        return AdmissionResult(
            admitted=False, reason="OpenSpec change not found", category="blocked"
        )

    # Run the vendored ecosystem validator
    errors = validate_change(change.change_dir)

    if errors:
        reasons = [f"[{e.artifact}] {e.reason}" for e in errors]
        return AdmissionResult(
            admitted=False,
            reason=f"Validation failed: {'; '.join(reasons[:3])}{' ...' if len(reasons) > 3 else ''}",
            change_id=change.change_id,
            category="blocked",
        )

    # Check for placeholder-only sections (verdict-core specific)
    placeholder_errors = _check_placeholder_sections(change.change_dir)

    if placeholder_errors:
        return AdmissionResult(
            admitted=False,
            reason=f"PLACEHOLDER content: {'; '.join(placeholder_errors[:2])}{' ...' if len(placeholder_errors) > 2 else ''}",
            change_id=change.change_id,
            category="blocked",
        )

    return AdmissionResult(
        admitted=True,
        reason="Change validated and admitted",
        change_id=change.change_id,
        category="ok",
    )


def spec_revision_digest(change: OpenSpecChange) -> str:
    """Compute a deterministic digest of the change's artifact files.

    Uses SHA256 over sorted POSIX relative paths and raw file bytes.
    Skips __pycache__ and dotfiles other than .openspec.yaml.

    Args:
        change: OpenSpec change

    Returns:
        Hex-encoded SHA256 digest
    """
    hasher = hashlib.sha256()

    # Collect all files, sorting by POSIX path for portability
    artifact_files = []
    for filepath in change.change_dir.rglob("*"):
        if not filepath.is_file():
            continue

        rel_path = filepath.relative_to(change.change_dir)
        posix_path = rel_path.as_posix()

        # Skip __pycache__ and dotfiles (except .openspec.yaml)
        if "__pycache__" in posix_path:
            continue
        parts = posix_path.split("/")
        if any(part.startswith(".") and part != ".openspec.yaml" for part in parts):
            continue

        artifact_files.append((posix_path, filepath))

    # Sort by POSIX path and hash
    artifact_files.sort(key=lambda x: x[0])

    for posix_path, filepath in artifact_files:
        # Hash the POSIX relative path
        hasher.update(posix_path.encode("utf-8"))
        hasher.update(b"\x00")  # Separator
        # Hash the file contents
        hasher.update(filepath.read_bytes())
        hasher.update(b"\x00")  # Separator

    return hasher.hexdigest()


def can_archive_openspec_change(
    change_id: str, has_merged_main_verification: bool
) -> tuple[bool, str]:
    """Check if an OpenSpec change can be archived.

    Archive is allowed ONLY when merged-main verification evidence is present.
    This is a precondition check; the actual `openspec archive` call is a thin
    wrapper around the CLI.

    Args:
        change_id: OpenSpec change identifier
        has_merged_main_verification: Whether merged-main verification passed

    Returns:
        Tuple of (can_archive, reason)
    """
    if not has_merged_main_verification:
        return (False, f"Cannot archive {change_id}: missing merged-main verification evidence")

    return (True, f"Change {change_id} ready for archive")
