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

Module level: a ``verdict/`` module is an orphan when nothing outside
``tests/`` reaches it by import or module-name string, starting from
``pyproject.toml`` entry points, ``verdict/__main__.py`` and every non-test
file outside ``verdict/`` (scripts, examples). Known orphans are listed in
``reachability-modules-baseline.json``.

Forbidden symbols (``reachability-forbidden.json``): a top-level symbol that
production code may reference only from its ``allowed_modules``. Any other
reference fails; the list is not baselined.

Ratchet (``--ratchet-base REF``, used on pull requests): both baselines may
only shrink. CI fails on a baseline entry that is not in REF, on a stale entry
(fixed code must leave the baseline), on an ``UNTRIAGED`` entry whose module
the branch changed, and on a forbidden symbol's ``allowed_modules`` growing or
going stale. ``--min-burn N`` also requires the ``UNTRIAGED`` count to fall by
at least N (integration trains).

Usage:
    scripts/check_reachability.py [--json] [--baseline PATH] [--skip-vulture]
                                  [--ratchet-base REF [--min-burn N]]

Exit codes:
    0: no NEW unreachable/stub-default/orphan entries beyond the baselines,
       no forbidden reference, and the ratchet holds
    1: new entries, a forbidden reference, or a ratchet violation
    2: usage/parse error
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
BASELINE_NAME = "reachability-baseline.json"
MODULE_BASELINE_NAME = "reachability-modules-baseline.json"
FORBIDDEN_NAME = "reachability-forbidden.json"
STUB_DEFAULT_HEALTH = {"unavailable", "disabled", "off", "unhealthy"}
UNTRIAGED = "UNTRIAGED"


class BaselineError(ValueError):
    """Raised when a reachability baseline is malformed or unsafe."""


@dataclass(frozen=True)
class BaselineEntry:
    qualified_name: str
    reason: str


@dataclass(frozen=True)
class ForbiddenSymbol:
    symbol: str
    allowed_modules: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Finding:
    qualified_name: str
    kind: str  # "uncalled" | "test_only_caller" | "stub_default" | "orphan_module"
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
    module_findings: tuple[Finding, ...] = ()
    new_module_findings: tuple[Finding, ...] = ()
    stale_module_entries: tuple[str, ...] = ()
    # "<symbol> in <module>" for production references outside allowed_modules.
    forbidden_references: tuple[str, ...] = ()
    # {symbol: production modules that reference it}, for the ratchet.
    forbidden_usage: dict[str, tuple[str, ...]] = field(default_factory=dict)
    public_symbols: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_symbols": self.checked_symbols,
            "findings": [f.to_dict() for f in self.findings],
            "new_findings": [f.to_dict() for f in self.new_findings],
            "stale_baseline_entries": list(self.stale_baseline_entries),
            "module_findings": [f.to_dict() for f in self.module_findings],
            "new_module_findings": [f.to_dict() for f in self.new_module_findings],
            "stale_module_entries": list(self.stale_module_entries),
            "forbidden_references": list(self.forbidden_references),
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


def load_forbidden(path: Path | None) -> tuple[ForbiddenSymbol, ...]:
    """Symbols that production code may reference only from ``allowed_modules``."""
    if path is None or not path.exists():
        return ()
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read forbidden symbols {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise BaselineError("forbidden symbols must be a JSON array")
    entries: list[ForbiddenSymbol] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or set(item) != {"symbol", "allowed_modules", "reason"}:
            raise BaselineError(
                f"forbidden entry {index} must contain exactly symbol, allowed_modules and reason"
            )
        symbol, allowed, reason = item["symbol"], item["allowed_modules"], item["reason"]
        if not isinstance(symbol, str) or not symbol.startswith("verdict."):
            raise BaselineError(f"forbidden entry {index} needs a verdict.* symbol")
        if not isinstance(allowed, list) or not all(isinstance(m, str) for m in allowed):
            raise BaselineError(f"forbidden entry {index} ({symbol}) needs a list of modules")
        if not isinstance(reason, str) or not reason:
            raise BaselineError(f"forbidden entry {index} ({symbol}) needs a non-empty reason")
        entries.append(ForbiddenSymbol(symbol, tuple(sorted(allowed)), reason))
    return tuple(entries)


def _prefixes(dotted: str) -> set[str]:
    parts = dotted.split(".")
    return {".".join(parts[: index + 1]) for index in range(len(parts))}


def _module_dependencies(tree: ast.Module, relative_path: str) -> set[str]:
    """Modules this file imports, plus ``verdict.*`` module-name strings it holds."""
    module = _module_dotted(relative_path)
    package = module if relative_path.endswith("__init__.py") else module.rpartition(".")[0]
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps |= _prefixes(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                source = ".".join([*parts, *([node.module] if node.module else [])])
            else:
                source = node.module or ""
            deps |= _prefixes(source)
            deps |= {f"{source}.{alias.name}" for alias in node.names}
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("verdict.")
            and " " not in node.value
        ):
            deps |= _prefixes(node.value.partition(":")[0])
    return deps - {module}


def _orphan_modules(trees: dict[str, ast.Module], entry_points: list[str]) -> list[Finding]:
    """``verdict/`` modules that no production root reaches by import or module string."""
    edges: dict[str, set[str]] = {}
    roots: set[str] = set()
    for relative_path, tree in trees.items():
        if _is_test_path(relative_path):
            continue
        module = _module_dotted(relative_path)
        edges[module] = _module_dependencies(tree, relative_path)
        if not _is_verdict_path(relative_path) or relative_path == "verdict/__main__.py":
            roots.add(module)
    for target in entry_points:
        roots |= _prefixes(target.partition(":")[0])
    reached: set[str] = set()
    pending = list(roots)
    while pending:
        module = pending.pop()
        if module not in reached:
            reached.add(module)
            pending.extend(edges.get(module, ()))
    return [
        Finding(
            _module_dotted(path),
            "orphan_module",
            path,
            1,
            "no production importer, entry point or module-name string reaches this module",
        )
        for path in sorted(trees)
        if _is_verdict_path(path) and _module_dotted(path) not in reached
    ]


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
    """Scan tracked ``verdict/`` files for unreachable symbols and modules."""
    root = root.resolve()
    if baseline_path is None:
        baseline_path = root / BASELINE_NAME
    baseline = {entry.qualified_name for entry in load_baseline(baseline_path)}
    module_baseline = {
        entry.qualified_name
        for entry in load_baseline(baseline_path.with_name(MODULE_BASELINE_NAME))
    }
    forbidden = load_forbidden(baseline_path.with_name(FORBIDDEN_NAME))
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
    forbidden_usage: dict[str, set[str]] = {entry.symbol: set() for entry in forbidden}
    for relative_path, tree in trees.items():
        if relative_path == ".vulture-whitelist.py":
            continue
        is_test = _is_test_path(relative_path)
        for name in _referenced_names(tree, relative_path, module_symbols):
            bucket = referenced_in.setdefault(name, {"prod": False, "test": False})
            bucket["test" if is_test else "prod"] = True
            module = _module_dotted(relative_path)
            if not is_test and name in forbidden_usage and module != name.rpartition(".")[0]:
                forbidden_usage[name].add(module)

    pyproject = root / "pyproject.toml"
    entry_points: list[str] = []
    if pyproject.exists():
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        groups = [project.get("scripts", {}), *project.get("entry-points", {}).values()]
        entry_points = [
            target for group in groups for target in group.values() if isinstance(target, str)
        ]
        for target in entry_points:
            if ":" in target:
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
    public_symbols: set[str] = set()
    checked = 0
    for relative_path, tree in trees.items():
        if not _is_verdict_path(relative_path):
            continue
        findings.extend(_stub_default_findings(tree, relative_path))
        for definition in _public_definitions(tree, relative_path):
            checked += 1
            qualified_name = f"{_module_dotted(relative_path)}.{definition.name}"
            public_symbols.add(qualified_name)
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

    modules = tuple(_orphan_modules(trees, entry_points))
    new_modules = tuple(f for f in modules if f.qualified_name not in module_baseline)
    stale_modules = tuple(sorted(module_baseline - {f.qualified_name for f in modules}))
    forbidden_refs = tuple(
        f"{entry.symbol} in {module}"
        for entry in forbidden
        for module in sorted(forbidden_usage[entry.symbol] - set(entry.allowed_modules))
    )
    return CheckResult(
        not (new_ordered or new_modules or forbidden_refs),
        checked,
        ordered,
        new_ordered,
        stale,
        modules,
        new_modules,
        stale_modules,
        forbidden_refs,
        {symbol: tuple(sorted(used)) for symbol, used in forbidden_usage.items()},
        frozenset(public_symbols),
    )


def _load_at(root: Path, ref: str, name: str) -> str | None:
    """``name`` as committed at ``ref``, or None when the file does not exist there."""
    shown = subprocess.run(
        ["git", "show", f"{ref}:{name}"], cwd=root, capture_output=True, text=True, check=False
    )
    return shown.stdout if shown.returncode == 0 else None


def ratchet(root: Path, result: CheckResult, base: str, *, min_burn: int = 0) -> list[str]:
    """Violations of "the baselines only shrink" between ``base`` and the working tree.

    A baseline file that does not exist at ``base`` is being seeded; its entries
    are accepted once. Every later change must remove entries, not add them.
    """
    root = root.resolve()
    # Compare with the fork point: a branch that is behind its base must not be
    # blamed for entries that the base removed after the branch started.
    fork = subprocess.run(
        ["git", "merge-base", base, "HEAD"], cwd=root, capture_output=True, text=True, check=False
    ).stdout.strip()
    if not fork:
        raise RuntimeError(f"unknown ratchet base {base!r}")
    with tempfile.TemporaryDirectory() as scratch:

        def at_base(name: str, loader: Any) -> Any:
            text = _load_at(root, fork, name)
            if text is None:
                return None
            path = Path(scratch) / name
            path.write_text(text, encoding="utf-8")
            return loader(path)

        old_symbols = at_base(BASELINE_NAME, load_baseline)
        old_modules = at_base(MODULE_BASELINE_NAME, load_baseline)
        old_forbidden = at_base(FORBIDDEN_NAME, load_forbidden)

    errors: list[str] = []
    current = {
        BASELINE_NAME: load_baseline(root / BASELINE_NAME),
        MODULE_BASELINE_NAME: load_baseline(root / MODULE_BASELINE_NAME),
    }
    for name, old in ((BASELINE_NAME, old_symbols), (MODULE_BASELINE_NAME, old_modules)):
        if old is None:
            continue
        before = {entry.qualified_name for entry in old}
        for entry in current[name]:
            if entry.qualified_name not in before:
                errors.append(
                    f"{name}: new entry {entry.qualified_name} (the baseline may only shrink)"
                )
    for stale in (*result.stale_baseline_entries, *result.stale_module_entries):
        errors.append(f"stale baseline entry {stale}: the finding is gone, remove the entry")

    touched = {
        _module_dotted(path)
        for path in subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if _is_verdict_path(path) and path.endswith(".py")
    }
    untriaged_now = 0
    for name, entries in current.items():
        for entry in entries:
            if not entry.reason.startswith(UNTRIAGED):
                continue
            untriaged_now += 1
            module = (
                entry.qualified_name
                if name == MODULE_BASELINE_NAME
                else (entry.qualified_name.rpartition(".")[0])
            )
            if module in touched:
                errors.append(
                    f"{entry.qualified_name} is UNTRIAGED in a module this branch changed "
                    f"({module}): wire it, delete it, or give it a triage reason"
                )
    if min_burn:
        untriaged_before = sum(
            entry.reason.startswith(UNTRIAGED)
            for old in (old_symbols, old_modules)
            for entry in (old or ())
        )
        if untriaged_before - untriaged_now < min_burn:
            errors.append(
                f"UNTRIAGED fell by {untriaged_before - untriaged_now} "
                f"({untriaged_before} -> {untriaged_now}); this branch must burn at least {min_burn}"
            )

    current_forbidden = {rule.symbol: rule for rule in load_forbidden(root / FORBIDDEN_NAME)}
    for old_entry in old_forbidden or ():
        rule = current_forbidden.get(old_entry.symbol)
        if rule is None:
            if old_entry.symbol in result.public_symbols:
                errors.append(f"{FORBIDDEN_NAME}: {old_entry.symbol} was removed but still exists")
            continue
        grown = set(rule.allowed_modules) - set(old_entry.allowed_modules)
        if grown:
            errors.append(
                f"{FORBIDDEN_NAME}: {rule.symbol} allowed_modules grew: {', '.join(sorted(grown))}"
            )
    for symbol, rule in current_forbidden.items():
        unused = set(rule.allowed_modules) - set(result.forbidden_usage.get(symbol, ()))
        if unused:
            errors.append(
                f"{FORBIDDEN_NAME}: {symbol} is no longer used by {', '.join(sorted(unused))}; "
                "remove them from allowed_modules"
            )
    return errors


def render_text(result: CheckResult) -> str:
    lines = [
        f"reachability check: {result.checked_symbols} public symbols, "
        f"{len(result.findings)} finding(s), {len(result.new_findings)} NEW (not baselined); "
        f"{len(result.module_findings)} orphan module(s), "
        f"{len(result.new_module_findings)} NEW"
    ]
    for finding in (*result.findings, *result.module_findings):
        new = finding in result.new_findings or finding in result.new_module_findings
        lines.append(
            f"- [{'NEW' if new else 'baselined'}] {finding.kind}: {finding.qualified_name} "
            f"({finding.file}:{finding.line}) -- {finding.detail}"
        )
    for reference in result.forbidden_references:
        lines.append(f"- [FORBIDDEN] {reference} (see {FORBIDDEN_NAME})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--skip-vulture",
        action="store_false",
        dest="use_vulture",
        default=True,
        help="Skip the vulture cross-check (e.g. if vulture is not installed).",
    )
    parser.add_argument(
        "--ratchet-base", metavar="REF", help="fail unless the baselines only shrank since REF"
    )
    parser.add_argument(
        "--min-burn",
        type=int,
        default=0,
        metavar="N",
        help="with --ratchet-base: UNTRIAGED must fall by at least N",
    )
    args = parser.parse_args(argv)
    if args.min_burn and not args.ratchet_base:
        parser.error("--min-burn needs --ratchet-base")
    try:
        result = check(baseline_path=args.baseline, use_vulture=args.use_vulture)
        violations = (
            ratchet(ROOT, result, args.ratchet_base, min_burn=args.min_burn)
            if args.ratchet_base
            else []
        )
    except (BaselineError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        if args.json_output:
            print(json.dumps({"error": str(exc), "ok": False}, indent=2, sort_keys=True))
        else:
            print(f"reachability check error: {exc}", file=sys.stderr)
        return 2
    stale = (*result.stale_baseline_entries, *result.stale_module_entries)
    if stale and not args.ratchet_base:
        print("reachability warning: stale baseline entries: " + ", ".join(stale), file=sys.stderr)
    if args.json_output:
        print(json.dumps({**result.to_dict(), "ratchet": violations}, indent=2, sort_keys=True))
    else:
        print(render_text(result), end="")
        for violation in violations:
            print(f"- [RATCHET] {violation}")
    return 0 if result.ok and not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
