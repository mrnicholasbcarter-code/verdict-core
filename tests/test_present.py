"""verdict.present: shared human-output helpers for the CLI migration (BOD-187)."""

from __future__ import annotations

import io

from rich.console import Console

from verdict import present
from verdict.terminal_ui import TerminalUI


def _capture(terminal: bool = False) -> io.StringIO:
    buf = io.StringIO()
    console = Console(
        file=buf,
        width=100,
        height=50,
        force_terminal=terminal,
        color_system="truecolor" if terminal else None,
    )
    present.reset(TerminalUI(console))
    return buf


def test_plain_output_has_no_escape_codes_and_sanitizes() -> None:
    buf = _capture()
    present.header("Models")
    present.kv({"id": "a\x1b[31mb", "tier": 2})
    present.table(["ID", "State"], [["m1", "healthy"], ["m2", None]])
    present.ok("catalog", "2 models")
    out = buf.getvalue()
    assert "\x1b[" not in out
    assert "VERDICT" in out and "m1" in out and "catalog" in out and "OK" in out


def test_empty_table_shows_empty_message() -> None:
    buf = _capture()
    present.table(["A"], [], empty="no receipts")
    assert "no receipts" in buf.getvalue()


def test_machine_mode_is_silent() -> None:
    buf = io.StringIO()
    present.reset(TerminalUI(Console(file=buf), machine=True))
    present.header("x")
    present.kv({"a": 1})
    present.table(["a"], [[1]])
    assert buf.getvalue() == ""
    present.reset(None)
