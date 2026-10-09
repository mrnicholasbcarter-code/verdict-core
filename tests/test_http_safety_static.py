"""Guard authenticated urllib call sites, including aliases and opener defaults."""

from __future__ import annotations

import ast
from pathlib import Path

# These health requests deliberately send Accept only, even though other
# functions in the same module authenticate. Do not change credential-free IO.
_ALLOWED = {
    (
        "doctor_diagnostics.py",
        "_omniroute_api_request",
        "health_req",
    ): "Local auto-detection health Request has only an Accept header.",
    (
        "doctor_diagnostics.py",
        "_collect_doctor_diagnostics",
        "health_req",
    ): "Configured gateway health Request has only an Accept header.",
    (
        "actions/registry.py",
        "_action_catalog",
        "request",
    ): "Public/management catalog Request has Accept only; inference uses safe transport.",
    (
        "cli.py",
        "cmd_autodev_packet_execute",
        "request",
    ): "Catalog Request has Accept only; the following probe uses safe transport.",
}
_UNSAFE = {"urllib.request.urlopen", "urllib.request.build_opener"}


def unsafe_references(source: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return ""

    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, function: str = "", argument: str = "") -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            argument = ast.unparse(node.args[0]) if node.args else ""
        if qualified(node) in _UNSAFE:
            found.append((function, argument, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function, argument)

    visit(tree)
    return found


def test_authenticated_modules_never_use_default_urllib_opener() -> None:
    root = Path(__file__).resolve().parents[1] / "verdict"
    violations: list[str] = []
    used: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        if path == root / "http_safety.py":
            continue  # Owns the private no-redirect build_opener.
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        authenticates = any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower() == "authorization"
            for node in ast.walk(tree)
        )
        # Explicit opener overrides in patch/decomposition wrappers are also
        # credential-bearing, although the transport builds Authorization.
        authenticates |= any(
            isinstance(node, ast.Call)
            and any(keyword.arg == "api_key" for keyword in node.keywords)
            for node in ast.walk(tree)
        )
        if not authenticates:
            continue
        for function, argument, lineno in unsafe_references(source):
            key = (path.relative_to(root).as_posix(), function, argument)
            if key in _ALLOWED:
                used.add(key)
            else:
                violations.append(f"{path.relative_to(root)}:{lineno}: {function}")
    assert not violations, "Credential-bearing default urllib openers:\n" + "\n".join(violations)
    assert used == set(_ALLOWED), "Remove stale credential-free allowlist entries"


def test_guard_resolves_aliases_and_opener_defaults() -> None:
    source = """
import urllib.request as http
from urllib.request import urlopen as send
import urllib

def probe(opener=send):
    send(request, timeout=1)
    http.urlopen(request, timeout=1)
    urllib.request.build_opener()
"""
    assert len(unsafe_references(source)) == 4
