"""`verdict` home screen."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from verdict.home import PALETTE, HomeState, recent_runs, render_home, run_home


def _console(width: int = 110, terminal: bool = False) -> Console:
    return Console(
        file=io.StringIO(),
        width=width,
        height=200,
        force_terminal=terminal,
        color_system="truecolor" if terminal else None,
    )


def test_palette_commands_are_registered_subcommands() -> None:
    import verdict.cli as cli

    parser_commands: set[str] = set()
    original = argparse.ArgumentParser.parse_args

    def capture(self: argparse.ArgumentParser, *a: object, **k: object) -> argparse.Namespace:
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                parser_commands.update(action.choices)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture  # type: ignore[method-assign]
    try:
        with contextlib.suppress(SystemExit):
            cli.main()
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    missing = [command for _, command, _ in PALETTE if command not in parser_commands]
    assert not missing, missing


def test_plain_home_has_no_escape_codes_and_lists_commands(tmp_path: Path) -> None:
    console = _console()
    assert run_home(console=console, runs_roots=[tmp_path], probe=False, animate=False) == 0
    text = console.file.getvalue()
    assert "\x1b[" not in text
    assert "VERDICT" in text and "verdict orchestrate" in text and "none yet" in text


def test_recent_runs_reads_receipt_outcome(tmp_path: Path) -> None:
    run = tmp_path / "r1"
    run.mkdir()
    (run / "events.jsonl").write_text("{}\n")
    (run / "receipt.json").write_text(json.dumps({"outcome": "COMPLETE", "reason": "ok"}))
    rows = recent_runs([tmp_path])
    assert rows[0]["run"] == "r1" and rows[0]["outcome"] == "COMPLETE"


def test_styled_home_shows_wordmark_and_gateway_state() -> None:
    state = HomeState(gateway="http://gw", gateway_ok=True, gateway_models=42)
    console = _console(terminal=True)
    console.print(render_home(state, plain=False, width=110))
    text = console.file.getvalue()
    assert "reachable" in text and "42 models" in text and "\u2588" in text


def test_hostile_run_names_are_sanitized(tmp_path: Path) -> None:
    run = tmp_path / "bad\x1b[31mname"
    run.mkdir()
    (run / "events.jsonl").write_text("{}\n")
    console = _console()
    run_home(console=console, runs_roots=[tmp_path], probe=False, animate=False)
    assert "\x1b[31m" not in console.file.getvalue()


def test_bare_verdict_without_tty_keeps_help_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import verdict.cli as cli

    monkeypatch.setattr(cli.sys, "argv", ["verdict"])
    cli.main()
    assert "Available commands" in capsys.readouterr().out


def test_python_dash_m_verdict_cli_still_runs_main() -> None:
    """`python -m verdict.cli` is a documented entry point; its __main__ guard must survive refactors."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "verdict.cli", "--help"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0 and "orchestrate" in proc.stdout
