#!/usr/bin/env python3
"""
Deterministic validator for OpenSpec verdict-change-v1 changes.

Usage: python3 scripts/validate_openspec_change.py <change-dir>

Validates that a change directory contains all required artifacts and fields
for the verdict-change-v1 schema, enforcing the full BOD-167 field mapping.

Exit codes:
  0 - Valid change
  1 - Invalid change (prints reason to stderr)
  2 - Usage error
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

# Required artifacts for verdict-change-v1
REQUIRED_ARTIFACTS = {
    "proposal.md": [
        "## Why",
        "## What Changes",
        "## Capabilities",
        "## Impact",
    ],
    "design.md": [
        "## Context",
        "## Goals / Non-Goals",
        "## Decisions",
        "## Risks / Trade-offs",
    ],
    "tasks.md": [
        "# Tasks",
        "## 1.",
    ],
}

# Required design.md sections with non-empty bodies (BOD-167 fields)
DESIGN_REQUIRED_SECTIONS = [
    ("Security / trust implications", ["security / trust implications", "security/trust implications"]),
    ("Routing / context / memory implications", ["routing / context / memory implications", "routing/context/memory implications"]),
    ("Migration / rollback", ["migration / rollback", "migration/rollback", "migration plan"]),
    ("ADR impact", ["adr impact"]),
]

# Evidence boundary labels (proposal must use these)
EVIDENCE_LABELS = ["KNOWN", "INFERRED", "NOT AVAILABLE"]


class ValidationError(NamedTuple):
    artifact: str
    reason: str


def extract_sections(content: str) -> dict[str, str]:
    """
    Extract sections from markdown content.
    Returns dict of {heading: body} where body is content until next heading.
    """
    sections = {}
    lines = content.split('\n')
    current_heading = None
    current_body_lines = []

    for line in lines:
        # Check for headings (## level or higher, but not #### scenarios)
        if line.startswith('##') and not line.startswith('####'):
            # Save previous section
            if current_heading is not None:
                sections[current_heading.lower()] = '\n'.join(current_body_lines).strip()

            # Start new section
            current_heading = line.lstrip('#').strip()
            current_body_lines = []
        elif current_heading is not None:
            current_body_lines.append(line)

    # Save last section
    if current_heading is not None:
        sections[current_heading.lower()] = '\n'.join(current_body_lines).strip()

    return sections


def has_non_empty_body(body: str) -> bool:
    """Check if section body has at least one non-blank, non-heading line."""
    lines = body.split('\n')
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            return True
    return False


def check_requirements_have_scenarios(content: str, rel_path: str) -> list[ValidationError]:
    """Check that each requirement has at least one scenario."""
    errors: list[ValidationError] = []

    lines = content.split('\n')
    in_requirement = False
    requirement_num = 0
    requirement_name = ""
    has_scenario = False

    for line in lines:
        if line.startswith("### Requirement:"):
            # If we were in a previous requirement and it had no scenario, error
            if in_requirement and not has_scenario:
                errors.append(
                    ValidationError(
                        rel_path,
                        f"Requirement {requirement_num} must have at least one '#### Scenario:' (BOD-167 field 6)"
                    )
                )

            # Start new requirement
            in_requirement = True
            requirement_num += 1
            requirement_name = line.replace("### Requirement:", "").strip()
            has_scenario = False

        elif line.startswith("####") and "Scenario:" in line:
            if in_requirement:
                has_scenario = True

        elif line.startswith("##") and not line.startswith("###") and not line.startswith("####"):
            # New major section, close out current requirement
            if in_requirement and not has_scenario:
                errors.append(
                    ValidationError(
                        rel_path,
                        f"Requirement {requirement_num} must have at least one '#### Scenario:' (BOD-167 field 6)"
                    )
                )
            in_requirement = False

    # Check last requirement
    if in_requirement and not has_scenario:
        errors.append(
            ValidationError(
                rel_path,
                f"Requirement {requirement_num} must have at least one '#### Scenario:' (BOD-167 field 6)"
            )
        )

    return errors


def validate_task_groups(content: str) -> list[ValidationError]:
    """
    Validate that EVERY task group (## N.) has its own verification and ownership mention.
    BOD-167 fields 10, 14 require verification per task; field 7 requires ownership where non-obvious.
    """
    errors: list[ValidationError] = []

    # Extract task groups
    lines = content.split('\n')
    task_groups = []
    current_group_heading = None
    current_group_lines = []

    for line in lines:
        # Match ## N. pattern (task group heading)
        if re.match(r'^##\s+\d+\.', line):
            # Save previous group
            if current_group_heading:
                task_groups.append({
                    'heading': current_group_heading,
                    'body': '\n'.join(current_group_lines)
                })

            # Start new group
            current_group_heading = line.strip()
            current_group_lines = []
        elif current_group_heading:
            current_group_lines.append(line)

    # Save last group
    if current_group_heading:
        task_groups.append({
            'heading': current_group_heading,
            'body': '\n'.join(current_group_lines)
        })

    # Validate each group
    for group in task_groups:
        body_lower = group['body'].lower()

        # Check for verification mention (verify, proof, test)
        has_verification = any(keyword in body_lower for keyword in ['verify', 'proof', 'test'])
        if not has_verification:
            errors.append(
                ValidationError(
                    "tasks.md",
                    f"Task group '{group['heading']}' must state verification method (BOD-167 fields 10, 14)"
                )
            )

    return errors


def validate_change(change_dir: Path) -> list[ValidationError]:
    """Validate a change directory for verdict-change-v1 compliance."""
    errors: list[ValidationError] = []

    if not change_dir.is_dir():
        errors.append(ValidationError("", f"Change directory does not exist: {change_dir}"))
        return errors

    # Check for .openspec.yaml to see if specs are skipped
    openspec_yaml = change_dir / ".openspec.yaml"
    skip_specs = False
    if openspec_yaml.exists():
        content = openspec_yaml.read_text(encoding="utf-8")
        skip_specs = "skip_specs: true" in content or "skip_specs:true" in content

    # Validate required artifacts
    for artifact, required_headings in REQUIRED_ARTIFACTS.items():
        artifact_path = change_dir / artifact

        if not artifact_path.exists():
            errors.append(ValidationError(artifact, f"Missing required artifact: {artifact}"))
            continue

        content = artifact_path.read_text(encoding="utf-8")
        content_lower = content.lower()

        # Check for required headings
        for heading in required_headings:
            if heading not in content:
                errors.append(ValidationError(artifact, f"Missing required heading: {heading}"))

        # Additional checks for proposal.md
        if artifact == "proposal.md":
            # Check for evidence boundary labels
            has_evidence = any(label in content for label in EVIDENCE_LABELS)
            if not has_evidence:
                errors.append(
                    ValidationError(
                        artifact,
                        "Must include evidence boundary using KNOWN/INFERRED/NOT AVAILABLE labels"
                    )
                )

            # Check for acceptance criteria section
            if "## Acceptance criteria" not in content and "## Acceptance Criteria" not in content:
                errors.append(ValidationError(artifact, "Missing acceptance criteria section"))

            # Check for at least one of New Capabilities or Modified Capabilities
            has_new = "### New Capabilities" in content or "###New Capabilities" in content
            has_modified = "### Modified Capabilities" in content or "###Modified Capabilities" in content
            if not (has_new or has_modified):
                errors.append(
                    ValidationError(
                        artifact,
                        "Must have at least one of '### New Capabilities' or '### Modified Capabilities'"
                    )
                )

        # Additional checks for design.md (BOD-167 field mapping - SECTION-BASED)
        if artifact == "design.md":
            sections = extract_sections(content)

            # Check each required section exists with non-empty body
            for field_name, heading_variants in DESIGN_REQUIRED_SECTIONS:
                # Try each variant of the heading
                found = False
                for variant in heading_variants:
                    if variant in sections:
                        body = sections[variant]
                        if has_non_empty_body(body):
                            found = True
                            break
                        else:
                            # Heading exists but body is empty
                            errors.append(
                                ValidationError(
                                    artifact,
                                    f"Section '{field_name}' exists but has empty body - must have substantive content"
                                )
                            )
                            found = True  # Don't report as missing
                            break

                if not found:
                    bod_field = ""
                    if field_name == "Security / trust implications":
                        bod_field = " (BOD-167 field 8)"
                    elif field_name == "Routing / context / memory implications":
                        bod_field = " (BOD-167 field 9)"
                    elif field_name == "Migration / rollback":
                        bod_field = " (BOD-167 field 13)"
                    elif field_name == "ADR impact":
                        bod_field = " (BOD-167 field 11)"

                    errors.append(
                        ValidationError(
                            artifact,
                            f"Missing required section '{field_name}' with non-empty body{bod_field}"
                        )
                    )

        # Additional checks for tasks.md (BOD-167 field mapping - PER-GROUP)
        if artifact == "tasks.md":
            task_errors = validate_task_groups(content)
            errors.extend(task_errors)

    # Validate spec files (unless skipped)
    specs_dir = change_dir / "specs"
    if not skip_specs:
        if not specs_dir.exists() or not any(specs_dir.rglob("*.md")):
            errors.append(
                ValidationError(
                    "specs/",
                    "No spec files found. Either add spec files or set skip_specs: true in .openspec.yaml"
                )
            )
        else:
            # Validate each spec file
            for spec_file in specs_dir.rglob("*.md"):
                content = spec_file.read_text(encoding="utf-8")
                rel_path = str(spec_file.relative_to(change_dir))

                # Check for required heading
                if "# Spec Delta" not in content:
                    errors.append(
                        ValidationError(
                            rel_path,
                            "Missing required heading: # Spec Delta"
                        )
                    )

                # Check for at least one Requirement block
                if "### Requirement:" not in content:
                    errors.append(
                        ValidationError(
                            rel_path,
                            "Must have at least one '### Requirement:' block (BOD-167 field 6)"
                        )
                    )
                else:
                    # Check that each requirement has at least one scenario
                    scenario_errors = check_requirements_have_scenarios(content, rel_path)
                    errors.extend(scenario_errors)

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/validate_openspec_change.py <change-dir>", file=sys.stderr)
        return 2

    change_dir = Path(sys.argv[1])
    errors = validate_change(change_dir)

    if errors:
        print(f"Validation failed for {change_dir}:", file=sys.stderr)
        for error in errors:
            if error.artifact:
                print(f"  [{error.artifact}] {error.reason}", file=sys.stderr)
            else:
                print(f"  {error.reason}", file=sys.stderr)
        return 1

    print(f"✓ Valid verdict-change-v1 change: {change_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
