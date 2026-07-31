#!/usr/bin/env python3
"""Check tracked production Python files against a structural line-count budget."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_MAX_LINES = 500
ROOT = Path(__file__).parents[1]
DEFAULT_BASELINE = ROOT / "config" / "structural_risk_baseline.json"


class BaselineError(ValueError):
    """Raised when a structural-risk baseline is malformed or unsafe."""


@dataclass(frozen=True)
class BaselineEntry:
    path: str
    max_lines: int
    rationale: str


@dataclass(frozen=True)
class Finding:
    path: str
    line_count: int
    max_lines: int
    kind: str


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    checked_files: int
    default_max_lines: int
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_files": self.checked_files,
            "default_max_lines": self.default_max_lines,
            "findings": [asdict(finding) for finding in self.findings],
            "ok": self.ok,
        }


def _validate_limit(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validate_baseline_path(value: object, *, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise BaselineError(f"baseline entry {index} path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) < 2
        or path.parts[0] != "verdict"
        or path.suffix != ".py"
        or "__pycache__" in path.parts
    ):
        raise BaselineError(f"baseline entry {index} has unsafe production path: {value!r}")
    return value


def load_baseline(path: Path | None) -> tuple[BaselineEntry, ...]:
    """Load and validate baseline entries from ``path``."""

    if path is None:
        return ()
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise BaselineError("baseline must be a JSON array")

    entries: list[BaselineEntry] = []
    seen: set[str] = set()
    required_fields = {"path", "max_lines", "rationale"}
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or set(item) != required_fields:
            raise BaselineError(
                f"baseline entry {index} must contain exactly path, max_lines, and rationale"
            )
        entry_path = _validate_baseline_path(item["path"], index=index)
        if entry_path in seen:
            raise BaselineError(f"baseline contains duplicate path: {entry_path}")
        seen.add(entry_path)
        try:
            max_lines = _validate_limit(
                item["max_lines"], field=f"baseline entry {index} max_lines"
            )
        except ValueError as exc:
            raise BaselineError(str(exc)) from exc
        rationale = item["rationale"]
        if not isinstance(rationale, str) or not rationale.strip():
            raise BaselineError(f"baseline entry {index} rationale must be a non-empty string")
        entries.append(BaselineEntry(entry_path, max_lines, rationale.strip()))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def tracked_python_files(root: Path) -> tuple[str, ...]:
    """Return sorted tracked production Python paths below ``verdict/``."""

    try:
        output = subprocess.run(
            ["git", "ls-files", "-z", "--", "verdict"], cwd=root, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot list tracked files below {root / 'verdict'}: {exc}") from exc

    paths = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("tracked verdict path is not valid UTF-8") from exc
        parsed = PurePosixPath(path)
        if parsed.suffix == ".py" and "__pycache__" not in parsed.parts:
            paths.append(path)
    return tuple(sorted(paths))


def check(
    root: Path = ROOT, *, max_lines: int = DEFAULT_MAX_LINES, baseline_path: Path | None = None
) -> CheckResult:
    """Check tracked production Python files and return deterministic findings."""

    root = root.resolve()
    _validate_limit(max_lines, field="max_lines")
    if baseline_path is None:
        default_baseline = root / "config" / "structural_risk_baseline.json"
        baseline_path = default_baseline if default_baseline.exists() else None
    baseline_entries = load_baseline(baseline_path)
    baseline = {entry.path: entry for entry in baseline_entries}
    tracked = tracked_python_files(root)
    tracked_set = set(tracked)
    findings: list[Finding] = []

    for stale_entry in baseline_entries:
        if stale_entry.path not in tracked_set:
            findings.append(Finding(stale_entry.path, 0, stale_entry.max_lines, "stale_baseline"))

    for relative_path in tracked:
        try:
            line_count = len((root / relative_path).read_bytes().splitlines())
        except OSError as exc:
            raise RuntimeError(f"cannot read tracked file {relative_path}: {exc}") from exc
        baseline_entry = baseline.get(relative_path)
        effective_limit = baseline_entry.max_lines if baseline_entry is not None else max_lines
        if line_count > effective_limit:
            findings.append(Finding(relative_path, line_count, effective_limit, "line_limit"))
        elif baseline_entry is not None and line_count <= max_lines:
            findings.append(Finding(relative_path, line_count, max_lines, "stale_baseline"))

    ordered = tuple(sorted(findings, key=lambda finding: (finding.path, finding.kind)))
    return CheckResult(not ordered, len(tracked), max_lines, ordered)


def render_json(result: CheckResult) -> str:
    """Render a stable JSON report."""

    return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"


def render_text(result: CheckResult) -> str:
    """Render a stable human-readable report."""

    if result.ok:
        return (
            f"structural risk check passed: {result.checked_files} tracked Python files "
            f"(default max {result.default_max_lines} lines)\n"
        )
    lines = ["structural risk check failed:"]
    for finding in result.findings:
        if finding.kind == "stale_baseline":
            lines.append(f"- {finding.path}: stale baseline entry")
        else:
            lines.append(
                f"- {finding.path}: {finding.line_count} lines exceeds max {finding.max_lines}"
            )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        result = check(max_lines=args.max_lines, baseline_path=args.baseline)
    except (BaselineError, RuntimeError, ValueError) as exc:
        if args.json_output:
            print(json.dumps({"error": str(exc), "ok": False}, indent=2, sort_keys=True))
        else:
            print(f"structural risk check error: {exc}", file=sys.stderr)
        return 2
    print(render_json(result) if args.json_output else render_text(result), end="")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
