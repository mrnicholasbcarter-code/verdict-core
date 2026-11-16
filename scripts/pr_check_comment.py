#!/usr/bin/env python3
"""Pull-request comment for the benchmark job: only what this pull request changed.

Compares the model-selection report built from the pull request with the one
built from its base commit, and reads the timing check report. An empty output
file means the pull request changed no result: the workflow then posts nothing
and removes its older comment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HEADING = "## Checks changed by this pull request"


def _load(path: Path) -> dict[str, Any] | None:
    """Return the report, or None when the step that writes it left no usable file."""
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text())
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _ms(value_ns: float) -> str:
    return f"{value_ns / 1_000_000:.2f} ms"


def timing_lines(gate: dict[str, Any] | None) -> list[str]:
    if gate is None:
        return ["**Timing check:** no report was produced, so it did not pass."]
    slow = [case for case in gate.get("benchmarks", []) if not case.get("threshold_passed")]
    if not slow:
        return []
    lines = [
        "**Timing check:** these cases ran slower than their limit.",
        "",
        "| Case | p95 | Limit |",
        "|---|---|---|",
    ]
    for case in slow:
        limit = case["thresholds"]["p95_ns_max"]
        lines.append(f"| `{case['name']}` | {_ms(case['summary']['p95'])} | {_ms(limit)} |")
    return lines


def _selected(report: dict[str, Any]) -> dict[str, str]:
    return {row["role"]: str(row["selected"]) for row in report.get("roles", [])}


def selection_lines(
    head: dict[str, Any] | None, base: dict[str, Any] | None
) -> tuple[list[str], str]:
    """Markdown lines for the comment, plus one line for the job log."""
    if head is None:
        return (
            ["**Model selection check:** no report was produced, so it did not pass."],
            "selection: no report for this pull request",
        )
    lines: list[str] = []
    violations = [str(item) for item in head.get("violations", [])]
    if violations:
        lines += ["**Model selection check failed:**", *(f"- {item}" for item in violations)]
    if base is None:
        return lines, "selection: not compared (no report for the base commit)"
    now, before = _selected(head), _selected(base)
    changed = [
        role for role in sorted(now.keys() | before.keys()) if now.get(role) != before.get(role)
    ]
    if changed:
        if lines:
            lines.append("")
        lines += [
            f"**Model selection changed** on the checked-in test inventory "
            f"`{head.get('fixture')}` (not the live gateway):",
            "",
            "| Role | Base | This pull request |",
            "|---|---|---|",
            *(
                f"| {role} | `{before.get(role, 'none')}` | `{now.get(role, 'none')}` |"
                for role in changed
            ),
        ]
    return lines, f"selection: compared with the base commit, {len(changed)} role(s) changed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-json", type=Path, required=True)
    parser.add_argument("--head-json", type=Path, required=True)
    parser.add_argument("--base-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)
    selection, note = selection_lines(_load(args.head_json), _load(args.base_json))
    sections = [block for block in (timing_lines(_load(args.gate_json)), selection) if block]
    body = ""
    if sections:
        body = "\n\n".join([HEADING, *("\n".join(block) for block in sections)]) + "\n"
    args.output_md.write_text(body)
    print(note)
    print(body or "No result changed: no comment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
