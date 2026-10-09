#!/usr/bin/env python3
"""Reachability report: "done means wired" (BOD-316).

Flags public top-level functions/classes in ``verdict/`` that have no
production caller, plus provider-stub factories whose default health leaves
them off by default. A caller inside ``tests/`` does not count as a
production caller.

``vulture`` (2.16 pinned in CI, >=2.16 in the dev dependencies) and
``code-review-graph dead-code`` both report unused code but do not distinguish
non-production references. This script resolves imports, aliases, same-module
names, registration decorators, entry points and literal ``getattr`` calls.
It is a TRIAGE signal, not a proof of dead code: runtime dispatch and dynamic
imports still need human review. See docs/quality/REACHABILITY-TRIAGE-2026-10.md.
The optional vulture cross-check uses ``python -m vulture verdict/
.vulture-whitelist.py --min-confidence 60`` with this interpreter. The whitelist
is vulture input only; it is not a production caller or a version pin.

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

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

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
    stale_baseline_entries: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_symbols": self.checked_symbols,
            "findings": [f.to_dict() for f in self.findings],
            "new_findings": [f.to_dict() for f in self.new_findings],
            "stale_baseline_entries": list(self.stale_baseline_entries),
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
    parts = PurePosixPath(relative_path).parts
    return (
        parts[:1] in {("tests",), ("benchmarks",)}
        or "fixtures" in parts
        or relative_path == ".vulture-whitelist.py"
    )


def _is_verdict_path(relative_path: str) -> bool:
    return PurePosixPath(relative_path).parts[:1] == ("verdict",)


def _module_dotted(relative_path: str) -> str:
    """The fully-qualified dotted module path for a tracked ``*.py`` file.

    ``verdict/release/normalize.py`` -> ``verdict.release.normalize``;
    ``verdict/release/__init__.py`` -> ``verdict.release``. Using the full
    path (not just ``Path(relative_path).stem``) avoids collapsing distinct
    modules that share a filename (e.g. ``verdict/normalize.py`` and
    ``verdict/release/normalize.py`` would otherwise both become
    ``verdict.normalize``).
    """
    parts = list(PurePosixPath(relative_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


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


def _referenced_names(tree: ast.Module, relative_path: str, module_symbols: set[str]) -> set[str]:
    """Resolve references through lexically scoped imports and top-level definitions."""
    module = _module_dotted(relative_path)
    local = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }

    def scoped_imports(body: list[ast.stmt]) -> dict[str, str]:
        imports: dict[str, str] = {}

        class Imports(ast.NodeVisitor):
            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if not node.level:
                    for alias in node.names:
                        imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"

            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                pass

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                pass

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                pass

        collector = Imports()
        for statement in body:
            collector.visit(statement)
        return imports

    names: set[str] = set()

    class References(ast.NodeVisitor):
        enclosing = ""
        imports = scoped_imports(tree.body)
        function_imports = imports

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            # Decorators, defaults and annotations belong to the enclosing scope.
            for child in [
                *node.decorator_list,
                node.args,
                node.returns,
                *getattr(node, "type_params", []),
            ]:
                if child is not None:
                    self.visit(child)
            previous = self.enclosing, self.imports, self.function_imports
            self.enclosing = node.name
            self.imports = {**self.function_imports, **scoped_imports(node.body)}
            self.function_imports = self.imports
            for statement in node.body:
                self.visit(statement)
            self.enclosing, self.imports, self.function_imports = previous

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for child in [
                *node.decorator_list,
                *node.bases,
                *node.keywords,
                *getattr(node, "type_params", []),
            ]:
                self.visit(child)
            previous = self.imports
            self.imports = {**self.imports, **scoped_imports(node.body)}
            for statement in node.body:
                self.visit(statement)
            self.imports = previous

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load):
                target = self.imports.get(node.id)
                if target:
                    names.add(target)
                elif node.id in local and node.id != self.enclosing:
                    names.add(f"{module}.{node.id}")

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[0].id in self.imports
            ):
                names.add(f"{self.imports[node.args[0].id]}.{node.args[1].value}")
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            parts = [node.attr]
            base = node.value
            while isinstance(base, ast.Attribute):
                parts.insert(0, base.attr)
                base = base.value
            if isinstance(base, ast.Name) and base.id in self.imports:
                names.add(".".join([self.imports[base.id], *parts]))
            # Keep name-only matching for methods, never module-level functions.
            elif node.attr not in module_symbols:
                names.add(f"method:{node.attr}")
            self.generic_visit(node)

    References().visit(tree)
    return names


def _stub_default_findings(tree: ast.Module, relative_path: str) -> list[Finding]:
    """Factory functions named ``make_*stub*`` whose ``health`` default is off."""
    findings: list[Finding] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        lname = node.name.lower()
        if not (lname.startswith("make_") and "stub" in lname):
            continue
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        default_args = positional[len(positional) - len(args.defaults) :]
        defaults = dict(zip((a.arg for a in default_args), args.defaults, strict=True))
        defaults.update(zip((a.arg for a in args.kwonlyargs), args.kw_defaults, strict=True))
        default = defaults.get("health")
        if (
            isinstance(default, ast.Constant)
            and isinstance(default.value, str)
            and default.value in STUB_DEFAULT_HEALTH
        ):
            findings.append(
                Finding(
                    qualified_name=f"{_module_dotted(relative_path)}.{node.name}",
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
    (rather than raising) if this interpreter has no ``vulture`` module, so
    this report degrades gracefully in environments without it installed.
    """
    whitelist = root / ".vulture-whitelist.py"
    args = [sys.executable, "-m", "vulture", "verdict/"]
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

    module_symbols = {
        n.name
        for tree in trees.values()
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    # referenced_in[qualified_name] -> {"prod": bool, "test": bool}
    referenced_in: dict[str, dict[str, bool]] = {}
    for relative_path, tree in trees.items():
        if relative_path == ".vulture-whitelist.py":
            continue
        is_test = _is_test_path(relative_path)
        for name in _referenced_names(tree, relative_path, module_symbols):
            bucket = referenced_in.setdefault(name, {"prod": False, "test": False})
            bucket["test" if is_test else "prod"] = True

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        scripts = (
            tomllib.loads(pyproject.read_text(encoding="utf-8"))
            .get("project", {})
            .get("scripts", {})
        )
        for target in scripts.values():
            if isinstance(target, str) and ":" in target:
                referenced_in[target.replace(":", ".")] = {"prod": True, "test": False}
    for relative_path, tree in trees.items():
        if _is_test_path(relative_path):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(d, ast.Call) for d in node.decorator_list
            ):
                referenced_in[f"{_module_dotted(relative_path)}.{node.name}"] = {
                    "prod": True,
                    "test": False,
                }

    findings: list[Finding] = []
    checked = 0
    for relative_path, tree in trees.items():
        if not _is_verdict_path(relative_path):
            continue
        findings.extend(_stub_default_findings(tree, relative_path))
        for definition in _public_definitions(tree, relative_path):
            checked += 1
            qualified_name = f"{_module_dotted(relative_path)}.{definition.name}"
            usage = referenced_in.get(qualified_name)
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
                        "only referenced from tests/ or other non-production paths",
                    )
                )

    ordered = tuple(sorted(findings, key=lambda f: (f.qualified_name, f.kind)))
    new_ordered = tuple(f for f in ordered if f.qualified_name not in baseline)
    stale = tuple(sorted(baseline - {f.qualified_name for f in ordered}))
    return CheckResult(not new_ordered, checked, ordered, new_ordered, stale)


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
    if result.stale_baseline_entries:
        print(
            "reachability warning: stale baseline entries: "
            + ", ".join(result.stale_baseline_entries),
            file=sys.stderr,
        )
    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_text(result), end="")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
