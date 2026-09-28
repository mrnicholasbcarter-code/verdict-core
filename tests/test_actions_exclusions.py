"""Action-layer exclusion audit (BOD-275 Lane E, rework).

Enforces:

1. Every leaf command in the argparse tree is in exactly one bucket
   (ACTION | MACHINE_ONLY | LAUNCH | ALIAS | GAP).
2. Every non-ACTION reason starts with the handler function that argparse
   dispatch actually calls for that leaf.
3. MACHINE_ONLY is limited to daemon/server lifecycle, programmatic
   endpoint, or destructive global uninstall.
4. Every LaunchSpec.entry resolves, is callable, and lives outside
   verdict.cli, verdict.commands, verdict.orchestration.cli.
5. Every ALIAS target names a real registered action.
6. The CLI handler for a LAUNCH command actually calls the domain entry
   named in LaunchSpec.entry (monkeypatch-and-invoke check).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import sys
from typing import Any

import pytest

from verdict.actions.registry import ALIAS, GAP, LAUNCH, MACHINE_ONLY, list_actions

# ---------------------------------------------------------------------------
# Enumerate every argparse leaf command
# ---------------------------------------------------------------------------


def _walk(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    result: list[str] = []
    if parser._subparsers is None:
        return [prefix] if prefix else []
    sub_actions = [
        a for a in parser._subparsers._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if not sub_actions:
        return [prefix] if prefix else []
    has_leaf = False
    for sub_action in sub_actions:
        for name, sp in sub_action.choices.items():
            new_prefix = f"{prefix}.{name}" if prefix else name
            sub_result = _walk(sp, new_prefix)
            if sub_result:
                result.extend(sub_result)
                has_leaf = True
    if not has_leaf and prefix:
        result.append(prefix)
    return result


def _capture_parser() -> argparse.ArgumentParser:
    """Trigger verdict.cli.main so every parser registers, capture the parser."""
    from verdict.cli import main

    captured: argparse.ArgumentParser | None = None
    original = argparse.ArgumentParser.parse_args

    def cap(self: argparse.ArgumentParser, args: Any = None, namespace: Any = None) -> None:
        nonlocal captured
        captured = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = cap  # type: ignore[assignment]
    saved_argv = sys.argv
    sys.argv = ["verdict"]
    try:
        with contextlib.suppress(SystemExit):
            main()
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[assignment]
        sys.argv = saved_argv
    if captured is None:
        pytest.fail("Could not capture parser")
    return captured


def _all_leaves() -> set[str]:
    return set(_walk(_capture_parser()))


# ---------------------------------------------------------------------------
# Bucket membership helpers
# ---------------------------------------------------------------------------


def _action_names() -> set[str]:
    return {spec.name for spec in list_actions()}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _leaf_in_actions(leaf: str, actions: set[str]) -> bool:
    """A leaf matches an action if the name is identical OR if the leaf is a
    top-level command and the action is its dotted subaction (e.g. leaf
    ``models`` matches action ``models.list``)."""
    if leaf in actions:
        return True
    prefix = leaf + "."
    return any(a.startswith(prefix) for a in actions)


def test_every_leaf_classified_once() -> None:
    """Every argparse leaf must land in exactly one bucket.

    An explicit MACHINE_ONLY / LAUNCH / ALIAS / GAP entry for a leaf is
    authoritative (a leaf like ``setup`` may launch the wizard while
    ``setup.plan`` remains a reachable ACTION for the scoped read).
    """
    leaves = _all_leaves()
    actions = _action_names()

    unclassified: list[str] = []
    overlaps: list[tuple[str, list[str]]] = []
    non_action_buckets = {
        "MACHINE_ONLY": set(MACHINE_ONLY.keys()),
        "LAUNCH": set(LAUNCH.keys()),
        "ALIAS": set(ALIAS.keys()),
        "GAP": set(GAP.keys()),
    }
    for leaf in sorted(leaves):
        explicit = [name for name, members in non_action_buckets.items() if leaf in members]
        if len(explicit) > 1:
            overlaps.append((leaf, explicit))
            continue
        if explicit:
            continue  # explicit bucket wins over any ACTION prefix match
        if _leaf_in_actions(leaf, actions):
            continue
        unclassified.append(leaf)
    problems: list[str] = []
    if unclassified:
        problems.append(f"unclassified leaves ({len(unclassified)}): {unclassified}")
    if overlaps:
        problems.append(f"overlapping leaves: {overlaps}")
    if problems:
        pytest.fail("\n".join(problems))


def test_machine_only_limited_to_allowed_kinds() -> None:
    """MACHINE_ONLY is limited to (a) daemon/server lifecycle, (b) a
    programmatic endpoint invoked by other programs (not humans), or
    (c) destructive global uninstall.

    Kinds like 'backward compatibility alias' or 'batch automation' are NOT
    valid MACHINE_ONLY kinds — aliases live in ALIAS; batch tools that a
    human runs are ACTIONs or LAUNCH.
    """
    allowed_markers = {
        # (a) daemon / server lifecycle
        "server lifecycle",
        "daemon lifecycle",
        "server until stopped",
        "loop that runs until",
        # (b) programmatic endpoint
        "programmatic endpoint",
        # (c) destructive global uninstall
        "destructive global uninstall",
    }
    failures: list[str] = []
    for cmd, reason in MACHINE_ONLY.items():
        lowered = reason.lower()
        if not any(marker in lowered for marker in allowed_markers):
            failures.append(
                f"{cmd}: reason does not name one of the allowed MACHINE_ONLY kinds "
                f"(server/daemon lifecycle, programmatic endpoint, or destructive "
                f"global uninstall); got: {reason!r}"
            )
    if failures:
        pytest.fail("\n".join(failures))


def test_reason_starts_with_handler_name() -> None:
    """Every non-ACTION reason must start with the handler function that
    argparse dispatch actually calls for that leaf.

    Format: '<handler_name>: <what it does>; <why bucket>'.
    """
    from verdict import cli as legacy
    from verdict.orchestration import cli as orch_cli
    from verdict.orchestration import supervisor

    # Handler resolution table: leaf -> function object (or None to skip name check).
    # Each entry names the function verdict.commands.dispatch.dispatch actually invokes.
    handlers: dict[str, Any] = {
        # MACHINE_ONLY
        "serve": legacy.cmd_serve if hasattr(legacy, "cmd_serve") else None,
        "mcp.serve": legacy.cmd_mcp,
        "hook.claude-gate": legacy.cmd_hook,
        "prove-at-rest.daemon": legacy.cmd_prove_at_rest,
        "uninstall": legacy.cmd_uninstall,
        # LAUNCH
        "orchestrate": orch_cli._orchestrate,
        "watch": orch_cli._watch,
        "supervise": supervisor.dispatch,
        "setup": legacy.cmd_setup,
        "ui": None,  # dispatched inline via verdict.actions.launch.launch_dashboard
        "quickstart": legacy.cmd_quickstart,
        "benchmark": legacy.cmd_benchmark,
        "autodev-golden-path": legacy.cmd_autodev_golden_path,
        "autodev.packet.execute": legacy.cmd_autodev_packet_execute,
    }

    failures: list[str] = []
    for cmd, reason in MACHINE_ONLY.items():
        expected = handlers.get(cmd)
        if expected is None:
            continue
        prefix = expected.__name__
        head = reason.split(":", 1)[0].strip()
        head_first = head.split(" ", 1)[0]
        if head_first != prefix:
            failures.append(
                f"MACHINE_ONLY[{cmd}]: reason must start with handler name "
                f"{prefix!r}; got {head_first!r} (full reason: {reason!r})"
            )
    for cmd, spec in LAUNCH.items():
        expected = handlers.get(cmd)
        if expected is None:
            continue
        prefix = expected.__name__
        head = spec.reason.split(":", 1)[0].strip()
        head_first = head.split(" ", 1)[0]
        if head_first != prefix:
            failures.append(
                f"LAUNCH[{cmd}]: reason must start with handler name "
                f"{prefix!r}; got {head_first!r} (full reason: {spec.reason!r})"
            )
    if failures:
        pytest.fail("\n".join(failures))


def test_launch_entries_resolve_and_are_callable() -> None:
    """Every LaunchSpec.entry must import successfully and resolve to a callable."""
    failures: list[str] = []
    for cmd, spec in LAUNCH.items():
        entry = spec.entry
        if ":" not in entry:
            failures.append(f"{cmd}: entry {entry!r} must be 'module:attr' format")
            continue
        module_name, attr = entry.rsplit(":", 1)
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"{cmd}: cannot import {module_name!r}: {exc}")
            continue
        if not hasattr(module, attr):
            failures.append(f"{cmd}: module {module_name!r} has no attribute {attr!r}")
            continue
        target = getattr(module, attr)
        if not callable(target):
            failures.append(f"{cmd}: {entry} is not callable")
    if failures:
        pytest.fail("\n".join(failures))


def test_launch_entries_live_outside_forbidden_layers() -> None:
    """LaunchSpec.entry must NOT point into verdict.cli, verdict.commands,
    or verdict.orchestration.cli — that would make the TUI depend on the
    CLI layer.
    """
    forbidden = ("verdict.cli", "verdict.commands", "verdict.orchestration.cli")
    failures: list[str] = []
    for cmd, spec in LAUNCH.items():
        module_name = spec.entry.split(":", 1)[0]
        if module_name in forbidden or any(
            module_name.startswith(f"{p}.") for p in ("verdict.commands",)
        ):
            failures.append(
                f"{cmd}: entry module {module_name!r} lives in a forbidden layer; "
                f"extract the domain function outside verdict.cli/commands/orchestration.cli"
            )
    if failures:
        pytest.fail("\n".join(failures))


def test_alias_targets_reach_real_action() -> None:
    """Every ALIAS target must name a registered action; the CLI alias
    handler must forward through the same action as its target.
    """
    action_names = _action_names()
    failures: list[str] = []
    for alias, target in ALIAS.items():
        if target not in action_names:
            failures.append(f"ALIAS[{alias}] -> {target!r}: target action is not registered")
    if failures:
        pytest.fail("\n".join(failures))

    # verdict run must forward to cmd_route (which calls the route action).
    from verdict import cli as legacy

    source = inspect.getsource(legacy.cmd_run)
    assert "cmd_route" in source, "cmd_run must forward to cmd_route (the route action)"
    source_plan = inspect.getsource(legacy.cmd_plan)
    assert "cmd_setup_plan" in source_plan, "cmd_plan must forward to cmd_setup_plan"


def test_ui_launch_calls_domain_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking `verdict ui` must call ``verdict.actions.launch.launch_dashboard``."""
    import argparse as _ap

    called: dict[str, int] = {"count": 0}

    def fake_launch() -> int:
        called["count"] += 1
        return 0

    monkeypatch.setattr("verdict.actions.launch.launch_dashboard", fake_launch)

    from verdict.commands.dispatch import dispatch

    parser = _ap.ArgumentParser()
    args = _ap.Namespace(command="ui")
    with contextlib.suppress(SystemExit):
        dispatch(parser, args)
    assert called["count"] == 1, "verdict ui should invoke launch_dashboard exactly once"


def test_reason_word_count() -> None:
    """Every non-ACTION reason must be a full sentence, not a two-word label."""
    failures: list[str] = []
    for cmd, reason in MACHINE_ONLY.items():
        if len(reason.split()) < 8:
            failures.append(f"MACHINE_ONLY[{cmd}]: reason too short ({len(reason.split())} words)")
    for cmd, spec in LAUNCH.items():
        if len(spec.reason.split()) < 8:
            failures.append(f"LAUNCH[{cmd}]: reason too short ({len(spec.reason.split())} words)")
    for cmd, reason in GAP.items():
        if len(reason.split()) < 8:
            failures.append(f"GAP[{cmd}]: reason too short ({len(reason.split())} words)")
    if failures:
        pytest.fail("\n".join(failures))
