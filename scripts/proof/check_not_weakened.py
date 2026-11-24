#!/usr/bin/env python3
"""Fail when a change weakens what the proof checks today (issue #1307, design D9).

    python scripts/proof/check_not_weakened.py --base origin/main [--repo PATH]

The checker resolves ``git merge-base <base> HEAD`` once. That commit is the
baseline for everything below and is also what lists the changed files, so a
change that landed on the base after the branch point is never blamed on the
branch and never hides one.

It exits 1 when any of these holds, and prints every finding:

* A skip, expected failure, deselect or marker is added to or removed from a real
  test. Every changed Python file under ``tests/`` (and a root ``conftest.py``),
  added, modified or deleted, is parsed with ``ast`` at the baseline and at HEAD.
  The constructs collected are calls, decorators and references that name
  ``skip``, ``skipif``, ``xfail``, ``importorskip``, ``pytest.mark.<name>`` other
  than ``parametrize`` (also through ``from pytest import mark``), the
  ``unittest`` skip family, assignments to ``pytestmark``, ``collect_ignore`` and
  ``collect_ignore_glob``, and the hooks ``pytest_deselected``,
  ``pytest_collection_modifyitems`` and ``pytest_ignore_collect``. Each is keyed by
  file, enclosing definitions and its ``ast.unparse`` text, so reformatting does not
  count and words held only in strings, comments or docstrings are never read.
* ``proof/contract.yaml`` is weakened: a gate is removed, its ``command`` or
  ``targets_placeholder`` changed, its ``required`` flag goes from true (or absent)
  to false, a ``when`` condition is added or changed, it gains ``env_clear`` names,
  a mode or a phase of a mode is removed, or a gate that ran in a mode (with or
  without ``--llm-boundary``) no longer does. Added gates, phases and keys are fine.
* The ``[tool.pytest.ini_options]`` table of ``pyproject.toml`` differs, a
  ``pytest.ini`` appears or changes, or ``.test_durations`` differs.

What a construct's record holds. Its own text; the scope it sits in; the whole function it is in
(docstring aside: parameters and defaults, decorators, every statement, so that moving, adding or
changing any assignment, loop or guard in a function that skips is a finding, even where the
construct itself is untouched); the conditions around it (``if``, ``else``, ``while``,
conditional expressions, the operands before it in ``and`` / ``or``); and what the class and
module constants read by the construct, its conditions or that function were bound to, each
followed through the names it reads in turn. So ``if False: skip()`` becoming ``if True:``, a
flag flipping, or ``SKIP = False`` becoming ``SKIP = True`` under an unchanged ``skipif(SKIP)``
are findings. A hook is recorded as its whole definition, docstring aside, with the constants it
reads. This errs on the side of reporting: an edit inside a test that already skips is reviewed
by a person, never passed silently.

What it does not see: it reads code, not behaviour. A guard that depends on something outside
the changed file (a constant imported from another module, an environment variable, a fixture's
value, a plugin, data a test reads) can change without changing any text this reads. Review
such edits by hand. Code that builds a skip dynamically (``getattr``, ``exec``) is out of reach.

Exit codes: 0 nothing weakened, 1 a weakening, 2 a git, usage or parse failure
(never a pass: a file that cannot be read or parsed is a failure to check).
"""

from __future__ import annotations

import argparse
import ast
import copy
import os
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

# Ensure the repository root is importable when this file runs as a script.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.proof.contract import ContractError, load_contract  # noqa: E402

EXIT_OK, EXIT_WEAKENED, EXIT_ERROR = 0, 1, 2

CONTRACT_PATH = "proof/contract.yaml"
PYPROJECT_PATH = "pyproject.toml"
DURATIONS_PATH = ".test_durations"
PYTEST_INI_FILES = ("pytest.ini", ".pytest.ini")
PYTEST_TABLE = ("tool", "pytest", "ini_options")

SKIP_NAMES = frozenset(
    {
        "skip",
        "skipif",
        "skipIf",
        "skipUnless",
        "xfail",
        "importorskip",
        "skipTest",
        "expectedFailure",
        "SkipTest",
    }
)
HOOK_NAMES = frozenset(
    {"pytest_deselected", "pytest_collection_modifyitems", "pytest_ignore_collect"}
)
ASSIGN_NAMES = frozenset({"pytestmark", "collect_ignore", "collect_ignore_glob"})
# Applying a marker to a test at run time, by name or object (request.node.add_marker('skip')).
MARKER_APPLIERS = frozenset({"add_marker", "applymarker"})
# ``config.option.<name> = ...`` changes what pytest selects.
SELECTION_OPTIONS = frozenset({"markexpr", "keyword", "deselect", "ignore", "ignore_glob"})
SKIP_MODULES = frozenset({"pytest", "unittest", "unittest.case"})

Construct = tuple[tuple[str, ...], str, str]


# Variables git sets for a hook (``git rev-parse --local-env-vars``). Inherited by a
# subprocess they override ``git -C`` and ``cwd=``, so the checker removes them: it must
# read the repository it was given, never the one a hook happens to run in.
GIT_LOCAL_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)


def git_environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV}


class CheckError(Exception):
    """git, usage or parse failure: the change could not be checked."""


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False, env=git_environment()
        )
    except OSError as exc:
        raise CheckError(f"git could not run: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise CheckError(f"git {' '.join(args)} failed: {detail}")
    return done.stdout


def _exists(repo: Path, rev: str, path: str) -> bool:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{rev}:{path}"],
            capture_output=True,
            check=False,
            env=git_environment(),
        )
    except OSError as exc:
        raise CheckError(f"git could not run: {exc}") from exc
    return done.returncode == 0


def _show(repo: Path, rev: str, path: str) -> bytes | None:
    """The file at a revision, or ``None`` when it does not exist there."""
    if not _exists(repo, rev, path):
        return None
    return _git(repo, "show", f"{rev}:{path}")


def merge_base(repo: Path, base: str) -> str:
    return _git(repo, "merge-base", base, "HEAD").decode("utf-8").strip()


def changed_files(repo: Path, baseline: str) -> dict[str, str]:
    """Path -> git status letter (A, M, D, T) between the baseline and HEAD."""
    raw = _git(repo, "diff", "--name-status", "--no-renames", "-z", baseline, "HEAD")
    parts = [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p != ""]
    if len(parts) % 2:
        raise CheckError("git diff --name-status produced an odd number of fields")
    return {parts[i + 1]: parts[i][:1] for i in range(0, len(parts), 2)}


# ---------------------------------------------------------------------------
# Tests: skip-like constructs found by parsing
# ---------------------------------------------------------------------------


def _simple_bindings(body: list[ast.stmt]) -> dict[str, ast.expr]:
    """``name = value`` statements of one scope, not those of the definitions nested in it."""
    found: dict[str, ast.expr] = {}
    pending = list(body)
    while pending:
        stmt = pending.pop(0)
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            target = value = None
        if isinstance(target, ast.Name) and value is not None:
            found[target.id] = value
        pending.extend(c for c in ast.iter_child_nodes(stmt) if isinstance(c, ast.stmt))
    return found


def _without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
    """A copy of a function without its docstring: wording is not behaviour."""
    clone = copy.deepcopy(node)
    first = clone.body[0] if clone.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        clone.body = clone.body[1:] or [ast.Pass()]
    return clone


class _Collector(ast.NodeVisitor):
    def __init__(self, tree: ast.AST) -> None:
        self.scope: list[str] = []
        self.found: Counter[Construct] = Counter()
        self.mark_aliases: set[str] = {"mark"}
        self.name_aliases: dict[str, str] = {}
        # What a skip depends on besides its own text: the conditions around it, and the
        # module-level constants those conditions and the construct itself read.
        self.conditions: list[tuple[str, frozenset[str]]] = []
        self.bindings: list[dict[str, ast.expr]] = []
        # The outermost function being visited: a construct inside it is recorded with the whole
        # function, so any edit in that function (an assignment before or after the guard, an
        # augmented write, a loop iterable, a default, a decorator) changes the record.
        self.outer: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        self.outer_text = ""
        self.constants: dict[str, ast.expr] = _simple_bindings(
            tree.body if isinstance(tree, ast.Module) else []
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in SKIP_MODULES:
                for alias in node.names:
                    if alias.name == "mark" and node.module == "pytest":
                        self.mark_aliases.add(alias.asname or alias.name)
                    elif alias.name in SKIP_NAMES:
                        self.name_aliases[alias.asname or alias.name] = alias.name
                    elif alias.name == "*":
                        for name in SKIP_NAMES:
                            self.name_aliases.setdefault(name, name)

    @staticmethod
    def _names(node: ast.AST) -> frozenset[str]:
        """Names a piece of code reads, and the attributes it reads from ``self`` or ``cls``."""
        found: set[str] = set()
        for part in ast.walk(node):
            if isinstance(part, ast.Name):
                found.add(part.id)
            elif (
                isinstance(part, ast.Attribute)
                and isinstance(part.value, ast.Name)
                and part.value.id in {"self", "cls"}
            ):
                found.add(part.attr)
        return frozenset(found)

    def _binding(self, name: str) -> ast.expr | None:
        """The value a name was given at class or module level, innermost scope first."""
        for scope in reversed(self.bindings):
            if name in scope:
                return scope[name]
        return self.constants.get(name)

    def _dependencies(self, names: set[str]) -> list[str]:
        """What ``names`` were bound to (class and module level), and so on in turn."""
        seen: dict[str, ast.expr] = {}
        pending = list(names)
        while pending:
            name = pending.pop()
            value = None if name in seen else self._binding(name)
            if value is None:
                continue
            seen[name] = value
            pending.extend(self._names(value))
        return sorted(f"{n} = {ast.unparse(v)}" for n, v in seen.items())

    def _record(self, kind: str, text: str, node: ast.AST | None = None) -> None:
        names: set[str] = set(self._names(node)) if node is not None else set()
        for _, read in self.conditions:
            names |= read
        suffix = ""
        if self.outer is not None:
            names |= self._names(self.outer)
            suffix += f" [in: {self.outer_text}]"
        if self.conditions:
            suffix += " [under: " + "; ".join(c for c, _ in self.conditions) + "]"
        dependencies = self._dependencies(names)
        if dependencies:
            suffix += " [with: " + "; ".join(dependencies) + "]"
        self.found[(tuple(self.scope), kind, text + suffix)] += 1

    def _under(self, label: str, test: ast.expr, body: list[ast.stmt]) -> None:
        self.conditions.append((f"{label} {ast.unparse(test)}", self._names(test)))
        for statement in body:
            self.visit(statement)
        self.conditions.pop()

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._under("if", node.test, node.body)
        self._under("else of", node.test, node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._under("while", node.test, node.body)
        self._under("else of", node.test, node.orelse)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        """``guard and skip()``: an operand runs only if the ones before it let it."""
        label = "and" if isinstance(node.op, ast.And) else "or"
        before: list[ast.expr] = []
        for operand in node.values:
            if before:
                text = f" {label} ".join(ast.unparse(b) for b in before)
                read = frozenset().union(*(self._names(b) for b in before))
                self.conditions.append((f"{label}-guard {text}", read))
                self.visit(operand)
                self.conditions.pop()
            else:
                self.visit(operand)
            before.append(operand)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        for label, branch in (("if", node.body), ("else of", node.orelse)):
            self.conditions.append((f"{label} {ast.unparse(node.test)}", self._names(node.test)))
            self.visit(branch)
            self.conditions.pop()

    def _resolved(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return self.name_aliases.get(node.id, node.id)
        return None

    def _alias_note(self, node: ast.expr) -> str:
        """What an imported name stands for, so re-pointing the import changes the record."""
        if isinstance(node, ast.Name):
            target = self.name_aliases.get(node.id)
            if target is not None and target != node.id:
                return f" -> {target}"
        return ""

    def _is_marker(self, node: ast.Attribute) -> bool:
        if node.attr == "parametrize":
            return False
        base = node.value
        if isinstance(base, ast.Attribute):
            return base.attr == "mark"
        return isinstance(base, ast.Name) and base.id in self.mark_aliases

    def _names_a_construct(self, node: ast.expr) -> bool:
        name = self._resolved(node)
        if name in SKIP_NAMES or name in HOOK_NAMES or name in MARKER_APPLIERS:
            return True
        return isinstance(node, ast.Attribute) and self._is_marker(node)

    def _visit_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        if not isinstance(node, ast.ClassDef) and node.name in HOOK_NAMES:
            # The whole definition, not its name: a hook that is edited to drop or ignore
            # tests keeps its name.
            self._record("hook definition", ast.unparse(_without_docstring(node)), node)
        self.scope.append(node.name)
        outermost = not isinstance(node, ast.ClassDef) and self.outer is None
        if outermost:
            self.outer, self.outer_text = node, ast.unparse(_without_docstring(node))  # type: ignore[arg-type]
        self.bindings.append(_simple_bindings(node.body) if isinstance(node, ast.ClassDef) else {})
        self.generic_visit(node)
        self.bindings.pop()
        if outermost:
            self.outer, self.outer_text = None, ""
        self.scope.pop()

    visit_FunctionDef = _visit_definition  # noqa: N815
    visit_AsyncFunctionDef = _visit_definition  # noqa: N815
    visit_ClassDef = _visit_definition  # noqa: N815

    @staticmethod
    def _is_protected(node: ast.expr) -> bool:
        """An exclusion or marker list that tests share: ``collect_ignore``, ``pytestmark``."""
        return (isinstance(node, ast.Name) and node.id in ASSIGN_NAMES) or (
            isinstance(node, ast.Attribute) and node.attr in ASSIGN_NAMES
        )

    @staticmethod
    def _is_selection_option(node: ast.AST) -> bool:
        """``config.option.markexpr``: the attribute pytest reads to decide what to run."""
        if not (isinstance(node, ast.Attribute) and node.attr in SELECTION_OPTIONS):
            return False
        holder = node.value
        return (isinstance(holder, ast.Attribute) and holder.attr == "option") or (
            isinstance(holder, ast.Name) and holder.id == "option"
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Recorded where it stands, with its scope, so that re-pointing an import (whatever
        # alias it takes, in whichever function) is a change even when its call sites are not.
        if node.module in SKIP_MODULES and any(
            alias.name in SKIP_NAMES or alias.name == "*" for alias in node.names
        ):
            self._record("import", ast.unparse(node))

    def visit_Delete(self, node: ast.Delete) -> None:
        if any(self._is_protected(part) for t in node.targets for part in ast.walk(t)):
            self._record("deletion", ast.unparse(node), node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and self._is_protected(node.func.value):
            # collect_ignore.append(...), pytestmark.extend(...): changed in place.
            self._record("call", ast.unparse(node), node)
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if self._names_a_construct(node.func):
            self._record("call", ast.unparse(node) + self._alias_note(node.func), node)
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._names_a_construct(node):
            self._record("reference", ast.unparse(node), node)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # A skip-like function imported from pytest or unittest, used without a call:
        # ``stop = skip``, ``raise SkipTest``, a decorator. Plain names are not flagged.
        if isinstance(node.ctx, ast.Load) and node.id in self.name_aliases:
            self._record("reference", node.id + self._alias_note(node), node)

    def _visit_assignment(self, node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for part in ast.walk(target):
                if (
                    (isinstance(part, ast.Name) and part.id in ASSIGN_NAMES)
                    or (isinstance(part, ast.Attribute) and part.attr in ASSIGN_NAMES)
                    or self._is_selection_option(part)
                ):
                    self._record("assignment", ast.unparse(node), node)
                    break
        self.generic_visit(node)

    visit_Assign = _visit_assignment  # noqa: N815
    visit_AnnAssign = _visit_assignment  # noqa: N815
    visit_AugAssign = _visit_assignment  # noqa: N815


def constructs_in(source: bytes | None, path: str) -> Counter[Construct]:
    """Skip-like constructs of one Python file, as a multiset; empty for an absent file."""
    if source is None:
        return Counter()
    try:
        tree = ast.parse(source.decode("utf-8"), filename=path)
    except (SyntaxError, ValueError, RecursionError, UnicodeDecodeError) as exc:
        raise CheckError(f"{path}: cannot be parsed ({type(exc).__name__}: {exc})") from exc
    collector = _Collector(tree)
    collector.visit(tree)
    return collector.found


def _is_checked_python(path: str) -> bool:
    return path.endswith(".py") and (path.startswith("tests/") or path == "conftest.py")


def test_findings(repo: Path, baseline: str, changed: Mapping[str, str]) -> list[str]:
    findings: list[str] = []
    for path in sorted(p for p in changed if _is_checked_python(p)):
        status = changed[path]
        before = constructs_in(None if status == "A" else _show(repo, baseline, path), path)
        after = constructs_in(None if status == "D" else _show(repo, "HEAD", path), path)
        for verb, delta in (("added", after - before), ("removed", before - after)):
            for (scope, kind, text), count in sorted(delta.items()):
                where = ".".join(scope) or "<module>"
                times = f" (x{count})" if count > 1 else ""
                shown = " ".join(text.split())
                shown = shown if len(shown) <= 240 else shown[:237] + "..."
                findings.append(f"{path}: in {where}: {kind} {verb}: {shown}{times}")
    return findings


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def _load_contract(source: bytes | None, label: str) -> dict[str, Any] | None:
    if source is None:
        return None
    try:
        loaded = yaml.safe_load(source.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise CheckError(f"{label}: cannot be parsed ({type(exc).__name__})") from exc
    if not isinstance(loaded, dict):
        raise CheckError(f"{label}: the contract is not a mapping")
    _validate_shape(loaded, label)
    _validate_with_loader(source, label)
    return loaded


def _validate_with_loader(source: bytes, label: str) -> None:
    """Hold the contract to the proof loader's own rules, not a copy of them.

    A contract ``scripts/proof/run.py`` would refuse (a repeated gate id, a blank command
    part, an unknown ``classify`` or ``shard`` word, a bad ``shards``) cannot be compared:
    it is a failure to check, never a pass.
    """
    with tempfile.TemporaryDirectory(prefix="proof-contract-") as tmp:
        path = Path(tmp) / "contract.yaml"
        path.write_bytes(source)
        try:
            load_contract(path)
        except ContractError as exc:
            raise CheckError(f"{label}: the proof loader rejects it: {exc}") from exc


def _validate_shape(contract: Mapping[str, Any], label: str) -> None:
    """Refuse a contract whose nested values the comparison could not read, before comparing."""
    gates = contract.get("gates")
    if gates is not None and not isinstance(gates, list):
        raise CheckError(f"{label}: gates must be a list")
    for position, gate in enumerate(gates or []):
        if not isinstance(gate, dict):
            raise CheckError(f"{label}: gates[{position}] must be a mapping")
        if not isinstance(gate.get("id"), str):
            raise CheckError(f"{label}: gates[{position}].id must be a string")
        env = gate.get("env_clear")
        if env is not None and not (isinstance(env, list) and all(isinstance(n, str) for n in env)):
            raise CheckError(f"{label}: gates[{position}].env_clear must be a list of strings")
        # The types the proof loader (scripts/proof/contract.py) requires: a contract it would
        # refuse is a failure to check, never a pass.
        command = gate.get("command")
        if not (isinstance(command, list) and command and all(isinstance(c, str) for c in command)):
            raise CheckError(
                f"{label}: gates[{position}].command must be a non-empty list of strings"
            )
        if not isinstance(gate.get("phase"), str):
            raise CheckError(f"{label}: gates[{position}].phase must be a string")
        if "required" in gate and not isinstance(gate["required"], bool):
            raise CheckError(f"{label}: gates[{position}].required must be a boolean")
        for key in ("when", "targets_placeholder"):
            if gate.get(key) is not None and not isinstance(gate[key], str):
                raise CheckError(f"{label}: gates[{position}].{key} must be a string")
    modes = contract.get("modes")
    if modes is not None and not isinstance(modes, dict):
        raise CheckError(f"{label}: modes must be a mapping")
    for name, config in (modes or {}).items():
        if not isinstance(config, dict):
            raise CheckError(f"{label}: modes.{name} must be a mapping")
        phases = config.get("phases")
        if phases is not None and not (
            isinstance(phases, list) and all(isinstance(p, str) for p in phases)
        ):
            raise CheckError(f"{label}: modes.{name}.phases must be a list of strings")


def effective_gates(contract: Mapping[str, Any], mode: str, llm_boundary: bool) -> set[str]:
    """The ids of the gates that run in a mode, read the way ``gates_for_mode`` reads them."""
    modes = contract.get("modes")
    config = modes.get(mode) if isinstance(modes, dict) else None
    phases = config.get("phases") if isinstance(config, dict) else None
    if not isinstance(phases, list):
        return set()
    wanted = {str(p) for p in phases}
    selected: set[str] = set()
    for gate in contract.get("gates") or []:
        if not isinstance(gate, dict) or str(gate.get("phase")) not in wanted:
            continue
        if gate.get("when") == "llm_boundary" and not llm_boundary:
            continue
        selected.add(str(gate.get("id")))
    return selected


def _gates_by_id(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    gates = contract.get("gates")
    return {
        g["id"]: g
        for g in (gates if isinstance(gates, list) else [])
        if isinstance(g, dict) and isinstance(g.get("id"), str)
    }


def contract_findings(base: Mapping[str, Any], head: Mapping[str, Any]) -> list[str]:
    where = CONTRACT_PATH
    findings: list[str] = []
    head_gates = _gates_by_id(head)
    for gate_id, old in _gates_by_id(base).items():
        new = head_gates.get(gate_id)
        if new is None:
            findings.append(f"{where}: gate {gate_id} removed")
            continue
        if new.get("command") != old.get("command"):
            findings.append(
                f"{where}: gate {gate_id} command changed: {old.get('command')} -> {new.get('command')}"
            )
        if old.get("required", True) is not False and new.get("required", True) is False:
            findings.append(f"{where}: gate {gate_id} required flag set to false")
        if new.get("when") is not None and new.get("when") != old.get("when"):
            findings.append(
                f"{where}: gate {gate_id} execution condition added or changed: "
                f"when {old.get('when')!r} -> {new.get('when')!r}"
            )
        if new.get("targets_placeholder") != old.get("targets_placeholder"):
            findings.append(f"{where}: gate {gate_id} targets_placeholder changed")
        added_env = set(new.get("env_clear") or []) - set(old.get("env_clear") or [])
        if added_env:
            findings.append(
                f"{where}: gate {gate_id} gained env_clear names {sorted(map(str, added_env))}"
            )
    base_modes = base.get("modes") if isinstance(base.get("modes"), dict) else {}
    head_modes = head.get("modes") if isinstance(head.get("modes"), dict) else {}
    for mode, config in base_modes.items():
        if mode not in head_modes:
            findings.append(f"{where}: mode {mode} removed")
            continue
        old_phases = config.get("phases") if isinstance(config, dict) else None
        new_config = head_modes[mode]
        new_phases = new_config.get("phases") if isinstance(new_config, dict) else None
        for phase in old_phases if isinstance(old_phases, list) else []:
            if not isinstance(new_phases, list) or phase not in new_phases:
                findings.append(f"{where}: phase {phase} removed from mode {mode}")
        ran = {flag: effective_gates(base, mode, flag) for flag in (False, True)}
        now = {flag: effective_gates(head, mode, flag) for flag in (False, True)}
        for gate_id in sorted(ran[False] - now[False]):
            findings.append(f"{where}: gate {gate_id} no longer runs in mode {mode}")
        for gate_id in sorted((ran[True] - now[True]) - (ran[False] - now[False])):
            findings.append(
                f"{where}: gate {gate_id} no longer runs in mode {mode} with --llm-boundary"
            )
    return findings


# ---------------------------------------------------------------------------
# pytest settings and the durations file
# ---------------------------------------------------------------------------


def _pytest_table(source: bytes | None, label: str) -> dict[str, Any]:
    if source is None:
        return {}
    try:
        loaded: Any = tomllib.loads(source.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CheckError(f"{label}: cannot be parsed ({type(exc).__name__})") from exc
    for key in PYTEST_TABLE:
        loaded = loaded.get(key) if isinstance(loaded, dict) else None
    return loaded if isinstance(loaded, dict) else {}


def settings_findings(repo: Path, baseline: str, changed: Mapping[str, str]) -> list[str]:
    findings: list[str] = []
    old = _pytest_table(_show(repo, baseline, PYPROJECT_PATH), f"{PYPROJECT_PATH} at the baseline")
    new = _pytest_table(_show(repo, "HEAD", PYPROJECT_PATH), f"{PYPROJECT_PATH} at HEAD")
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            findings.append(
                f"{PYPROJECT_PATH}: [tool.pytest.ini_options] {key} changed: "
                f"{old.get(key)!r} -> {new.get(key)!r}"
            )
    if _show(repo, baseline, DURATIONS_PATH) != _show(repo, "HEAD", DURATIONS_PATH):
        findings.append(f"{DURATIONS_PATH}: changed (no refresh belongs in this change)")
    for ini in PYTEST_INI_FILES:
        if ini in changed:
            findings.append(f"{ini}: pytest configuration file changed ({changed[ini]})")
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check(repo: Path, base: str) -> tuple[str, list[str]]:
    """The merge-base used and every finding; raises ``CheckError`` when it cannot check."""
    baseline = merge_base(repo, base)
    changed = changed_files(repo, baseline)
    findings = test_findings(repo, baseline, changed)
    old = _load_contract(_show(repo, baseline, CONTRACT_PATH), f"{CONTRACT_PATH} at the baseline")
    new = _load_contract(_show(repo, "HEAD", CONTRACT_PATH), f"{CONTRACT_PATH} at HEAD")
    if old is not None and new is None:
        findings.append(f"{CONTRACT_PATH}: deleted")
    elif old is not None and new is not None:
        findings.extend(contract_findings(old, new))
    findings.extend(settings_findings(repo, baseline, changed))
    return baseline, findings


def _lines(items: Iterable[str]) -> str:
    return "\n".join(f"  - {item}" for item in items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a change weakens the proof gate.")
    parser.add_argument("--base", required=True, help="Base ref, for example origin/main")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository (default: cwd)")
    args = parser.parse_args(argv)
    try:
        baseline, findings = check(args.repo, args.base)
    except CheckError as exc:
        print(f"check_not_weakened: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # a crash must never look like exit 1 (a weakening) or 0
        print(f"check_not_weakened: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if findings:
        print(f"check_not_weakened: {len(findings)} finding(s) against {baseline[:12]}:")
        print(_lines(findings))
        return EXIT_WEAKENED
    print(f"check_not_weakened: no weakening found (base {args.base}, merge-base {baseline[:12]})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
