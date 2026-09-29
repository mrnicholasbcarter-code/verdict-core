"""Doctor presentation consumes existing findings and adds safe repair guidance."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from verdict.design import PALETTE
from verdict.doctor_presentation import PROBLEM_STATES, repair_command
from verdict.terminal_ui import TerminalUI


def frame(ui: TerminalUI) -> None:
    ui.header("Doctor")
    ui.doctor(
        {
            "capabilities": [
                {
                    "capability_id": "code.symbols",
                    "status": "covered",
                    "selected_provider_id": "native",
                    "health": "healthy",
                    "authority": "local",
                },
                {"capability_id": "memory.search", "status": "missing", "health": "unknown"},
            ]
        }
    )
    ui.doctor_summary(["Missing configuration: verdict.yaml", "OmniRoute gateway unreachable"], [])


@pytest.mark.parametrize("width", [60, 100, 200])
@pytest.mark.parametrize("policy", ["plain", "no_color", "non_tty"])
def test_plain_golden(monkeypatch: pytest.MonkeyPatch, width: int, policy: str) -> None:
    for key in ("NO_COLOR", "CI", "TERM", "VERDICT_PLAIN"):
        monkeypatch.delenv(key, raising=False)
    if policy == "plain":
        monkeypatch.setenv("VERDICT_PLAIN", "1")
    if policy == "no_color":
        monkeypatch.setenv("NO_COLOR", "")
    output = io.StringIO()
    frame(TerminalUI(Console(file=output, width=width, force_terminal=policy != "non_tty")))
    text = output.getvalue()
    assert (
        text == (Path(__file__).parent / "golden" / "first_run" / f"doctor-{width}.txt").read_text()
    )
    assert "\x1b" not in text
    assert text.count("Repair:") == 3
    assert all(Text(line).cell_len <= width for line in text.splitlines())


def test_color_uses_shared_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("NO_COLOR", "CI", "VERDICT_PLAIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    output = io.StringIO()
    frame(
        TerminalUI(Console(file=output, force_terminal=True, width=100, color_system="truecolor"))
    )
    for token in ("PURPLE", "CYAN", "RED", "SURFACE"):
        assert ";".join(map(str, PALETTE[token].rgb)) in output.getvalue()


@pytest.mark.parametrize(
    "finding,command",
    [
        ("Configuration file (verdict.yaml) is missing.", "verdict setup"),
        ("Gateway unreachable", "verdict detect"),
        ("Required credential OPENAI_API_KEY is not set", "verdict credentials set OPENAI_API_KEY"),
        ("Config written by an older Verdict version", "verdict doctor --fix"),
        ("missing_memory_db_file", "verdict doctor --fix"),
        ("provider auth_failed", "verdict setup credentials"),
        ("memory.search missing", "verdict setup --recommended"),
    ],
)
def test_repair_commands(finding: str, command: str) -> None:
    assert repair_command(finding) == command


@pytest.mark.parametrize("state", sorted(PROBLEM_STATES))
def test_every_problem_has_repair_line(state: str) -> None:
    output = io.StringIO()
    ui = TerminalUI(Console(file=output, width=100, force_terminal=False))
    ui.doctor_finding("Provider", state, "Observed detail")
    assert state.upper() in output.getvalue()
    assert "Observed detail" in output.getvalue()
    assert "Repair: verdict " in output.getvalue()


def test_resolved_findings_do_not_request_repair() -> None:
    output = io.StringIO()
    ui = TerminalUI(Console(file=output, width=100, force_terminal=False))
    ui.doctor_summary(["configuration missing"], ["configuration"])
    assert "FIXED" in output.getvalue()
    assert "Repair:" not in output.getvalue()
