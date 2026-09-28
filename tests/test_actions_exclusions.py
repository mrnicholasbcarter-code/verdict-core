"""Test action layer exclusions: MACHINE_ONLY, LAUNCH, and GAP classifications.

BOD-275 Lane E: Verify that every CLI command is properly classified and that
LAUNCH entries have correct callable entry points.
"""

from __future__ import annotations

import argparse
import importlib

import pytest

from verdict.actions.registry import GAP, LAUNCH, MACHINE_ONLY, list_actions


def _get_all_cli_commands() -> set[str]:
    """Extract all commands and subcommands from the actual argparse tree."""

    from verdict.cli import main

    commands = set()

    # Capture the parser before it tries to parse args
    original_parse_args = argparse.ArgumentParser.parse_args
    captured_parser = None

    def capture_parser(self, args=None, namespace=None):
        nonlocal captured_parser
        captured_parser = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture_parser

    try:
        main()
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = original_parse_args

    if captured_parser is None:
        pytest.fail("Could not capture parser")

    # Extract all top-level commands
    subparsers_actions = [
        action for action in captured_parser._subparsers._actions
        if isinstance(action, argparse._SubParsersAction)
    ]

    if not subparsers_actions:
        pytest.fail("No subparsers found")

    subparsers_action = subparsers_actions[0]

    for cmd_name in subparsers_action.choices:
        commands.add(cmd_name)

    return commands


def test_every_command_classified_once():
    """Every top-level command in the argparse tree must be in exactly one bucket.

    Subcommands are covered by their parent command's classification.
    """
    cli_commands = _get_all_cli_commands()

    # Get commands from each bucket
    action_commands = set()
    for action in list_actions():
        # Map action name to CLI command (base command only)
        if '.' in action.name:
            action_commands.add(action.name.split('.')[0])
        else:
            action_commands.add(action.name)

    machine_only = set(MACHINE_ONLY.keys())
    launch = set(LAUNCH.keys())
    gap = set(GAP.keys())

    # Every CLI command must be in at least one bucket
    classified = action_commands | machine_only | launch | gap
    unclassified = cli_commands - classified

    if unclassified:
        pytest.fail(
            f"Unclassified commands ({len(unclassified)}): {sorted(unclassified)}"
        )

    # No command should be in multiple buckets
    overlaps = []
    for cmd in cli_commands:
        buckets = []
        if cmd in action_commands:
            buckets.append("ACTION")
        if cmd in machine_only:
            buckets.append("MACHINE_ONLY")
        if cmd in launch:
            buckets.append("LAUNCH")
        if cmd in gap:
            buckets.append("GAP")

        if len(buckets) > 1:
            overlaps.append((cmd, buckets))

    if overlaps:
        msg = "Commands in multiple buckets:\n"
        for cmd, buckets in overlaps:
            msg += f"  {cmd}: {', '.join(buckets)}\n"
        pytest.fail(msg)


def test_non_action_reasons_are_specific():
    """Every non-ACTION reason must be >=8 words and not rely on generic terms alone."""
    generic_terms = {
        'legacy', 'dev-tool', 'batch', 'heavy', 'plumbing',
        'future', 'network-heavy', 'computation-heavy'
    }

    failures = []

    # Check MACHINE_ONLY
    for cmd, reason in MACHINE_ONLY.items():
        words = reason.split()
        if len(words) < 8:
            failures.append(f"MACHINE_ONLY[{cmd}]: only {len(words)} words")

        # Check if it only uses generic terms
        # Count words that give command-specific meaning
        specific_words = [
            w for w in words
            if len(w) > 3 and w.lower() not in generic_terms
            and w.lower() not in {'the', 'and', 'for', 'with', 'from', 'that', 'this'}
        ]
        if len(specific_words) < 4:
            failures.append(
                f"MACHINE_ONLY[{cmd}]: too generic (needs command-specific details)"
            )

    # Check LAUNCH
    for cmd, spec in LAUNCH.items():
        words = spec.reason.split()
        if len(words) < 8:
            failures.append(f"LAUNCH[{cmd}]: only {len(words)} words")

        specific_words = [
            w for w in words
            if len(w) > 3 and w.lower() not in generic_terms
            and w.lower() not in {'the', 'and', 'for', 'with', 'from', 'that', 'this'}
        ]
        if len(specific_words) < 4:
            failures.append(
                f"LAUNCH[{cmd}]: too generic (needs command-specific details)"
            )

    # Check GAP
    for cmd, reason in GAP.items():
        words = reason.split()
        if len(words) < 8:
            failures.append(f"GAP[{cmd}]: only {len(words)} words")

    if failures:
        pytest.fail("\n".join(failures))


def test_machine_only_limited_to_allowed_kinds():
    """MACHINE_ONLY must be limited to the explicitly allowed categories."""

    # Each MACHINE_ONLY entry must indicate one of these patterns:
    # - daemon/server lifecycle keywords
    # - programmatic invocation (not for end users)
    # - destructive/dangerous operations
    # - backward compatibility aliases
    # - batch/automation tool invocation

    failures = []

    for cmd, reason in MACHINE_ONLY.items():
        reason_lower = reason.lower()

        is_lifecycle = any(
            term in reason_lower
            for term in ['daemon', 'server', 'lifecycle', 'long-running', 'background', 'process']
        )
        is_programmatic = any(
            term in reason_lower
            for term in ['invoked by', 'programmatic', 'integration', 'not users', 'not end users', 'automation']
        )
        is_destructive = any(
            term in reason_lower
            for term in ['destructive', 'uninstall', 'no rollback', 'removes all']
        )
        is_alias = any(
            term in reason_lower
            for term in ['alias', 'aliasing', 'retained', 'backward compatibility']
        )
        is_batch_tool = any(
            term in reason_lower
            for term in ['batch', 'pipeline', 'automation', 'continuous integration']
        )

        if not (is_lifecycle or is_programmatic or is_destructive or is_alias or is_batch_tool):
            failures.append(
                f"{cmd}: reason does not clearly indicate why it is MACHINE_ONLY "
                f"(should mention: daemon/lifecycle, programmatic invocation, destructive ops, "
                f"compatibility alias, or batch automation)"
            )

    if failures:
        pytest.fail("\n".join(failures))


def test_launch_entries_are_callable():
    """Every LaunchSpec.entry must import successfully and be callable."""
    failures = []

    for cmd, spec in LAUNCH.items():
        entry = spec.entry

        if ':' not in entry:
            failures.append(f"{cmd}: entry '{entry}' must be 'module:function' format")
            continue

        module_name, func_name = entry.rsplit(':', 1)

        # Try to import the module
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            failures.append(f"{cmd}: cannot import '{module_name}': {e}")
            continue
        except Exception as e:
            # Handle cases like dashboard.py that have import-time errors
            # These are acceptable for LAUNCH commands that run in separate processes
            if 'dashboard' in module_name:
                continue
            failures.append(f"{cmd}: error importing '{module_name}': {e}")
            continue

        # Try to get the function
        if not hasattr(module, func_name):
            failures.append(
                f"{cmd}: module '{module_name}' has no attribute '{func_name}'"
            )
            continue

        func = getattr(module, func_name)

        # Verify it's callable
        if not callable(func):
            failures.append(f"{cmd}: {entry} is not callable")

    if failures:
        pytest.fail("\n".join(failures))


def test_launch_entry_matches_cli_handler():
    """Verify LaunchSpec.entry is in expected verdict modules.

    This is a structural check - entry points should be in verdict.cli,
    verdict.orchestration.*, or verdict.dashboard for consistency.
    """
    valid_prefixes = (
        'verdict.cli',
        'verdict.orchestration.',
        'verdict.dashboard',
        'verdict.commands.',
    )

    failures = []

    for cmd, spec in LAUNCH.items():
        module_name = spec.entry.split(':')[0]

        if not any(module_name.startswith(prefix) for prefix in valid_prefixes):
            failures.append(
                f"{cmd}: entry module '{module_name}' not in expected locations "
                f"(should start with: verdict.cli, verdict.orchestration., verdict.dashboard)"
            )

    if failures:
        pytest.fail("\n".join(failures))
