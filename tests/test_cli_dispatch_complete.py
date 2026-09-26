"""Verify all top-level subparsers have corresponding dispatch branches."""

import ast
import re
from pathlib import Path


def test_all_subparsers_have_dispatch_branches() -> None:
    """Assert that every top-level subparser in cli.py has a dispatch handler."""
    cli_path = Path("verdict/cli.py")
    dispatch_path = Path("verdict/commands/dispatch.py")

    # Read files
    cli_source = cli_path.read_text(encoding="utf-8")
    dispatch_source = dispatch_path.read_text(encoding="utf-8")

    # Find all subparser registrations by looking for add_parser calls
    subparser_choices = _extract_subparser_choices(cli_source)

    # Get all handled commands from dispatch.py
    handled_commands = _extract_dispatched_commands(dispatch_source)

    # Find unhandled commands
    unhandled = []
    for name in subparser_choices:
        if name not in handled_commands:
            unhandled.append(name)

    # Report results
    if unhandled:
        msg = (
            f"Unsubatched subparsers: {', '.join(sorted(unhandled))}\n"
            f"Handled commands: {', '.join(sorted(handled_commands))}\n"
            f"Total subparsers: {len(subparser_choices)}, "
            f"Total handled: {len(handled_commands)}"
        )
        raise AssertionError(msg)

    # Verify compare is handled
    assert "compare" in handled_commands, "compare command must be handled"


def _extract_subparser_choices(source: str) -> list[str]:
    """Extract all subparser choices from cli.py source."""
    choices = []

    # Find the main function
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            # Look for parser.add_subparsers and then add_parser calls
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute) and stmt.func.attr == "add_parser" and stmt.args:
                    first_arg = stmt.args[0]
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                        choices.append(first_arg.value)

    return sorted(choices)


def _extract_dispatched_commands(source: str) -> list[str]:
    """Extract all commands handled in dispatch.py."""
    commands = []

    # Look for patterns like: args.command == "command_name"
    # and args.command in {..., "command_name", ...}
    pattern1 = r'args\.command\s*==\s*["\']([^"\']+)["\']'
    pattern2 = r'args\.command\s+in\s+\{([^}]+)\}'

    for match in re.finditer(pattern1, source):
        commands.append(match.group(1))

    for match in re.finditer(pattern2, source):
        # Extract individual command names from the set literal
        set_content = match.group(1)
        # Match quoted strings within the set
        for cmd_match in re.finditer(r'["\']([^"\']+)["\']', set_content):
            commands.append(cmd_match.group(1))

    return sorted(set(commands))
