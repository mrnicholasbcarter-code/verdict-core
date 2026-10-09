#!/usr/bin/env python3
"""Reachability report: "done means wired" (BOD-316).

Flags public top-level functions/classes in ``verdict/`` that have no
production caller, plus provider-stub factories whose default health leaves
them off by default. A caller inside ``tests/`` does not count as a
production caller.

``vulture`` (2.16, repo-pinned via ``.vulture-whitelist.py`` /
``pyproject.toml``) and ``code-review-graph dead-code`` both report *unused*
code but do not distinguish a test-only caller from a production one, which
is the exact distinction this story needs ("done means wired", not "done
means imported by a test"). This script therefore uses the stdlib ``ast``
module directly: it walks every tracked ``*.py`` file once, records which
identifiers are referenced from ``tests/`` vs. everywhere else, and reports
public ``verdict/`` symbols that are referenced only from tests (or not at
all). Detection is name-based (like vulture): it matches AST identifier
references by name, not by full type resolution, so it can under- or
over-report for very common names. It is a TRIAGE signal for human review,
not a proof of dead code -- see docs/quality/REACHABILITY-TRIAGE-2026-10.md
for the policy this feeds, and run ``vulture verdict/ .vulture-whitelist.py
--min-confidence 60`` or ``code-review-graph dead-code`` alongside it for a
second opinion during triage.

Usage:
    scripts/check_reachability.py [--json] [--baseline PATH] [--skip-vulture]

Exit codes:
    0: no NEW unreachable/stub-default entries beyond the baseline
    1: new entries found
    2: usage/parse error
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "reachability-baseline.json"
STUB_DEFAULT_HEALTH = {"unavailable", "disabled", "off", "unhealthy"}


class BaselineError(ValueError):
    """Raised when a reachability baseline is malformed or unsafe."""


@dataclass(frozen=True)
class BaselineEntry:
    qualified_name: str
    reason: str


@dataclass(frozen=True)
class Finding:
    qualified_name: str
    kind: str  # "uncalled" | "test_only_caller" | "stub_default"
    file: str
    line: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    checked_symbols: int
    findings: tuple[Finding, ...]
    new_findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_symbols": self.checked_symbols,
            "findings": [f.to_dict() for f in self.findings],
            "new_findings": [f.to_dict() for f in self.new_findings],
        }


def tracked_python_files(root: Path) -> tuple[str, ...]:
    """All tracked ``*.py`` files below ``root``, relative, POSIX-style."""
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z", "*.py"], cwd=root, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot list tracked files below {root}: {exc}") from exc
    paths = [p.decode("utf-8") for p in output.split(b"\0") if p]
    return tuple(sorted(paths))


def _is_test_path(relative_path: str) -> bool:
    return PurePosixPath(relative_path).parts[:1] == ("tests",)


def _is_verdict_path(relative_path: str) -> bool:
    return PurePosixPath(relative_path).parts[:1] == ("verdict",)


def _parse(root: Path, relative_path: str) -> ast.Module | None:
    try:
        source = (root / relative_path).read_text(encoding="utf-8")
        return ast.parse(source, filename=relative_path)
    except (OSError, SyntaxError, UnicodeError):
        return None


@dataclass(frozen=True)
class _Definition:
    name: str
    kind: str  # "function" | "class"
    file: str
    line: int


def _public_definitions(tree: ast.Module, relative_path: str) -> list[_Definition]:
    out: list[_Definition] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            out.append(_Definition(node.name, "function", relative_path, node.lineno))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            out.append(_Definition(node.name, "class", relative_path, node.lineno))
    return out


def _referenced_names(tree: ast.Module) -> set[str]:
    """Every identifier referenced by a Name(Load)/Attribute node in ``tree``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _stub_default_findings(tree: ast.Module, relative_path: str) -> list[Finding]:
    """Factory functions named ``make_*stub*`` whose ``health`` default is off."""
    findings: list[Finding] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        lname = node.name.lower()
        if not (lname.startswith("make_") and "stub" in lname):
            continue
        args = node.args
        kw_defaults = dict(zip(args.kwonlyargs, args.kw_defaults, strict=True))
        health_arg = next((a for a in args.kwonlyargs if a.arg == "health"), None)
        default = kw_defaults.get(health_arg) if health_arg is not None else None
        if (
            isinstance(default, ast.Constant)
            and isinstance(default.value, str)
            and default.value in STUB_DEFAULT_HEALTH
        ):
            findings.append(
                Finding(
                    qualified_name=f"verdict.{Path(relative_path).stem}.{node.name}",
                    kind="stub_default",
                    file=relative_path,
                    line=node.lineno,
                    detail=f"default health={default.value!r}; stub is off unless a caller overrides it",
                )
            )
    return findings


def load_baseline(path: Path | None) -> tuple[BaselineEntry, ...]:
    if path is None or not path.exists():
        return ()
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise BaselineError("baseline must be a JSON array")
    entries: list[BaselineEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or set(item) != {"qualified_name", "reason"}:
            raise BaselineError(
                f"baseline entry {index} must contain exactly qualified_name and reason"
            )
        qname, reason = item["qualified_name"], item["reason"]
        if not isinstance(qname, str) or not qname:
            raise BaselineError(f"baseline entry {index} has an empty qualified_name")
        if not isinstance(reason, str) or not reason:
            raise BaselineError(f"baseline entry {index} ({qname}) needs a non-empty reason")
        if qname in seen:
            raise BaselineError(f"baseline contains duplicate qualified_name: {qname}")
        seen.add(qname)
        entries.append(BaselineEntry(qname, reason))
    return tuple(entries)


def run_vulture(root: Path) -> dict[tuple[str, int], str]:
    """Run vulture 2.16 on verdict/ (whitelist-aware) and index its findings.

    Returns ``{(relative_path, line): message}`` for every line vulture
    reports as unused, at the same ``--min-confidence 60`` threshold used by
    the "test_only_caller"/"uncalled" distinction below. Vulture itself does
    not know about ``tests/`` vs. production callers (its "used" means "used
    anywhere, including tests"), so this is a corroborating second opinion,
    not the primary signal -- see the module docstring. Returns an empty dict
    (rather than raising) if the ``vulture`` executable is not on PATH, so
    this report degrades gracefully in environments without it installed.
    """
    whitelist = root / ".vulture-whitelist.py"
    args = ["vulture", "verdict/"]
    if whitelist.exists():
        args.append(str(whitelist))
    args += ["--min-confidence", "60"]
    try:
        proc = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    findings: dict[tuple[str, int], str] = {}
    for line in proc.stdout.splitlines():
        head, _, rest = line.partition(": ")
        file_part, _, line_part = head.rpartition(":")
        if not file_part or not line_part.isdigit():
            continue
        findings[(file_part, int(line_part))] = rest.strip()
    return findings


def check(
    root: Path = ROOT, *, baseline_path: Path | None = None, use_vulture: bool = True
) -> CheckResult:
    """Scan tracked ``verdict/`` files for unreachable public symbols."""
    root = root.resolve()
    if baseline_path is None:
        default_baseline = root / "reachability-baseline.json"
        baseline_path = default_baseline if default_baseline.exists() else None
    baseline = {entry.qualified_name for entry in load_baseline(baseline_path)}
    vulture_findings = run_vulture(root) if use_vulture else {}

    tracked = tracked_python_files(root)
    trees: dict[str, ast.Module] = {}
    for relative_path in tracked:
        tree = _parse(root, relative_path)
        if tree is not None:
            trees[relative_path] = tree

    # referenced_in[name] -> {"prod": bool, "test": bool}
    referenced_in: dict[str, dict[str, bool]] = {}
    for relative_path, tree in trees.items():
        is_test = _is_test_path(relative_path)
        for name in _referenced_names(tree):
            bucket = referenced_in.setdefault(name, {"prod": False, "test": False})
            bucket["test" if is_test else "prod"] = bucket["test" if is_test else "prod"] or True
            bucket["prod"] = bucket["prod"] or (not is_test)
            bucket["test"] = bucket["test"] or is_test

    findings: list[Finding] = []
    checked = 0
    for relative_path, tree in trees.items():
        if not _is_verdict_path(relative_path):
            continue
        findings.extend(_stub_default_findings(tree, relative_path))
        for definition in _public_definitions(tree, relative_path):
            checked += 1
            qualified_name = f"verdict.{PurePosixPath(relative_path).stem}.{definition.name}"
            usage = referenced_in.get(definition.name)
            if usage is None or not (usage["prod"] or usage["test"]):
                vulture_hit = vulture_findings.get((relative_path, definition.line))
                detail = "no reference found"
                if vulture_hit is not None:
                    detail += f"; vulture agrees: {vulture_hit}"
                findings.append(
                    Finding(qualified_name, "uncalled", relative_path, definition.line, detail)
                )
            elif usage["test"] and not usage["prod"]:
                findings.append(
                    Finding(
                        qualified_name,
                        "test_only_caller",
                        relative_path,
                        definition.line,
                        "only referenced from tests/",
                    )
                )

    ordered = tuple(sorted(findings, key=lambda f: (f.qualified_name, f.kind)))
    new_ordered = tuple(f for f in ordered if f.qualified_name not in baseline)
    return CheckResult(not new_ordered, checked, ordered, new_ordered)


def render_text(result: CheckResult) -> str:
    if not result.findings:
        return f"reachability check passed: {result.checked_symbols} public symbols checked, 0 findings\n"
    lines = [
        f"reachability check: {result.checked_symbols} public symbols, "
        f"{len(result.findings)} finding(s), {len(result.new_findings)} NEW (not baselined)"
    ]
    for finding in result.findings:
        marker = "NEW" if finding in result.new_findings else "baselined"
        lines.append(
            f"- [{marker}] {finding.kind}: {finding.qualified_name} "
            f"({finding.file}:{finding.line}) -- {finding.detail}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--skip-vulture",
        action="store_false",
        dest="use_vulture",
        default=True,
        help="Skip the vulture cross-check (e.g. if vulture is not installed).",
    )
    args = parser.parse_args(argv)
    try:
        result = check(baseline_path=args.baseline, use_vulture=args.use_vulture)
    except (BaselineError, RuntimeError, ValueError) as exc:
        if args.json_output:
            print(json.dumps({"error": str(exc), "ok": False}, indent=2, sort_keys=True))
        else:
            print(f"reachability check error: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_text(result), end="")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
