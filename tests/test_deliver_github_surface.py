"""The GitHub surface used by ``verdict deliver`` has no merge, approve, close or label verb.

Deny-list only: new methods (for example a mark-ready call) are allowed, but nothing
whose name contains a forbidden verb, and no ``gh`` argv literal for those verbs.
This test only imports and inspects; it runs no command and touches no network or home.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType
from typing import Any

import pytest

from verdict.orchestration import deliver_publish, ready_gate

FORBIDDEN_NAME_PARTS = ("merge", "approve", "close", "label")
FORBIDDEN_ARGV_LITERALS = frozenset({"merge", "--approve", "close", "--add-label", "add-label"})
MODULES = (deliver_publish, ready_gate)


def _public_callable_names(module: ModuleType) -> list[str]:
    """Public functions and classes defined in ``module``, plus their public methods."""
    names: list[str] = []
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        names.append(name)
        if inspect.isclass(obj):
            names.extend(
                f"{name}.{member}"
                for member, value in vars(obj).items()
                if not member.startswith("_") and callable(_unwrap(value))
            )
    return names


def _unwrap(value: Any) -> Any:
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def forbidden_names(names: list[str]) -> list[str]:
    return [
        name
        for name in names
        if any(part in name.rsplit(".", 1)[-1].lower() for part in FORBIDDEN_NAME_PARTS)
    ]


def forbidden_literals(source: str) -> list[str]:
    tree = ast.parse(source)
    return sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in FORBIDDEN_ARGV_LITERALS
        }
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_public_surface_has_no_forbidden_verb(module: ModuleType) -> None:
    names = _public_callable_names(module)
    assert names, f"no public callables found in {module.__name__}"
    assert forbidden_names(names) == []


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_source_has_no_forbidden_gh_argv_literal(module: ModuleType) -> None:
    source = inspect.getsource(module)
    assert forbidden_literals(source) == []


@pytest.mark.parametrize("verb", ["merge_pr", "approve", "close_issue", "add_label", "relabel"])
def test_deny_list_catches_a_forbidden_method_on_a_fake_client(verb: str) -> None:
    class FakeClient:
        def create_draft_pr(self) -> None: ...
        def mark_ready(self) -> None: ...

    setattr(FakeClient, verb, lambda self: None)
    names = [f"FakeClient.{m}" for m in vars(FakeClient) if not m.startswith("_")]
    assert forbidden_names(names) == [f"FakeClient.{verb}"]


def test_deny_list_allows_a_mark_ready_method() -> None:
    class FakeClient:
        def mark_ready(self) -> None: ...

    names = [f"FakeClient.{m}" for m in vars(FakeClient) if not m.startswith("_")]
    assert forbidden_names(names) == []


@pytest.mark.parametrize("verb", sorted(FORBIDDEN_ARGV_LITERALS))
def test_literal_check_catches_forbidden_gh_argv(verb: str) -> None:
    source = f'cmd = ["gh", "pr", "{verb}", "1"]\n'
    assert forbidden_literals(source) == [verb]
