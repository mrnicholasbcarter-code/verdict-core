"""Every registered top-level `verdict` subcommand must have a handler.

Regression for the `verdict compare` bug: the subparser was registered but
``verdict/commands/dispatch.py`` had no branch for it, so the CLI silently fell
through to ``parser.print_help()`` and exited 0.

The check builds the real parser (so it sees every registrar in
``verdict.commands.parsers_*`` plus ``verdict.orchestration.cli.add_parsers``)
and requires that each top-level choice is handled by one of the three real
dispatch mechanisms:

1. an explicit ``args.command == "<name>"`` / ``args.command in {...}`` branch
   in ``verdict/commands/dispatch.py``;
2. the orchestration handler table consulted first by ``dispatch()``
   (``verdict.orchestration.cli.dispatch`` -> ``handlers`` dict);
3. a ``func`` default on the subparser (``set_defaults(func=...)``).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from verdict.commands import (
    parsers_autodev,
    parsers_credentials,
    parsers_harness,
    parsers_models,
    parsers_openspec,
    parsers_routing,
    parsers_runtime,
    parsers_setup,
)

REPO = Path(__file__).resolve().parent.parent
DISPATCH_SRC = (REPO / "verdict" / "commands" / "dispatch.py").read_text(encoding="utf-8")
ORCH_SRC = (REPO / "verdict" / "orchestration" / "cli.py").read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    """Mirror ``verdict.cli.main`` parser construction without dispatching."""
    parser = argparse.ArgumentParser(description="Verdict: policy-gated LLM Router")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    for registrar in (
        parsers_setup,
        parsers_credentials,
        parsers_routing,
        parsers_autodev,
        parsers_harness,
        parsers_runtime,
        parsers_models,
        parsers_openspec,
    ):
        registrar.register(subparsers)
    return parser


def _top_level_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers registered on the top-level parser")


def _explicit_dispatch_branches(source: str) -> set[str]:
    handled: set[str] = set()
    handled.update(re.findall(r'args\.command\s*==\s*"([a-z0-9-]+)"', source))
    for group in re.findall(r"args\.command\s+in\s+\{([^}]*)\}", source):
        handled.update(re.findall(r'"([a-z0-9-]+)"', group))
    return handled


def _orchestration_handler_table(source: str) -> set[str]:
    """Keys of the ``handlers`` dict in ``verdict.orchestration.cli.dispatch``."""
    match = re.search(r"handlers\s*(?::[^=]+)?=\s*\{(.*?)\n\s*\}", source, re.S)
    assert match, "orchestration dispatch handler table not found"
    return set(re.findall(r'"([a-z0-9-]+)"\s*:', match.group(1)))


def test_every_top_level_subcommand_has_a_handler() -> None:
    parser = _build_parser()
    choices = _top_level_choices(parser)
    assert len(choices) >= 40, f"unexpectedly few subcommands registered: {sorted(choices)}"

    explicit = _explicit_dispatch_branches(DISPATCH_SRC)
    orchestration = _orchestration_handler_table(ORCH_SRC)

    unhandled = sorted(
        name
        for name, sub in choices.items()
        if name not in explicit and name not in orchestration and sub.get_default("func") is None
    )
    assert not unhandled, (
        f"registered subcommands with no handler (would print help and exit 0): {unhandled}"
    )


def test_compare_is_explicitly_dispatched() -> None:
    assert "compare" in _explicit_dispatch_branches(DISPATCH_SRC)


def test_detector_catches_a_missing_handler() -> None:
    """The check must actually fail when a subcommand has no handler."""
    parser = _build_parser()
    choices = _top_level_choices(parser)
    explicit = _explicit_dispatch_branches(DISPATCH_SRC) - {"compare"}
    orchestration = _orchestration_handler_table(ORCH_SRC)
    unhandled = [
        n
        for n, sub in choices.items()
        if n not in explicit and n not in orchestration and sub.get_default("func") is None
    ]
    assert unhandled == ["compare"]
