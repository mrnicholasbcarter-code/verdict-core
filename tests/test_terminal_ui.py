"""Presentation contracts: truthful outcomes and terminal safety."""

import io
import json

import pytest
from rich.console import Console

from verdict import terminal_ui


def presenter(monkeypatch, *, tty=True, width=80, env=None, machine=False):
    for key in ("NO_COLOR", "CI", "TERM", "VERDICT_NO_ANIMATION", "VERDICT_PLAIN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=tty, width=width, color_system="truecolor")
    return terminal_ui.TerminalUI(console, machine=machine), stream


@pytest.mark.parametrize(
    "env", [{"NO_COLOR": ""}, {"CI": "true"}, {"TERM": "dumb"}, {"VERDICT_PLAIN": "1"}]
)
def test_fallback_never_emits_controls(monkeypatch, env):
    ui, stream = presenter(monkeypatch, env=env)
    ui.header("Setup")
    with ui.task("Discovering"):
        ui.status("Python", "found")
    assert "Python" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_non_tty_retains_semantics_without_animation(monkeypatch):
    ui, stream = presenter(monkeypatch, tty=False)
    with ui.task("Discovering"):
        ui.status("Serena", "missing", "Optional semantic navigation")
    assert "MISSING" in stream.getvalue()
    assert "Optional semantic navigation" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


@pytest.mark.parametrize("width", [20, 40, 80, 120, 180])
def test_responsive_content_stays_within_terminal(monkeypatch, width):
    ui, stream = presenter(monkeypatch, width=width, env={"VERDICT_NO_ANIMATION": "1"})
    ui.header("Setup")
    ui.plan(
        {
            "actions": [
                {
                    "kind": "install_provider",
                    "description": "Install Serena semantic navigation",
                    "reason": "Missing symbols",
                    "requires_consent": True,
                }
            ]
        }
    )
    plain = Console(width=width)
    from rich.text import Text

    for line in stream.getvalue().splitlines():
        assert Text.from_ansi(line).cell_len <= width
    assert plain.width == width


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt])
def test_spinner_restores_cursor_and_never_claims_success(monkeypatch, error):
    ui, stream = presenter(monkeypatch)
    try:
        with ui.task("Installing Serena"):
            if error:
                raise error("interrupted")
    except (RuntimeError, KeyboardInterrupt):
        pass
    output = stream.getvalue()
    assert "\x1b[?25l" in output
    assert "\x1b[?25h" in output
    assert output.rfind("\x1b[?25h") > output.rfind("\x1b[?25l")
    assert "CERTIFIED" not in output


def test_machine_mode_is_silent_even_on_tty(monkeypatch):
    ui, stream = presenter(monkeypatch, machine=True)
    ui.header("Setup")
    with ui.task("Discovering"):
        ui.status("Python", "found")
    ui.plan({"actions": []})
    assert stream.getvalue() == ""


def test_status_treats_external_labels_as_literal_text(monkeypatch):
    ui, stream = presenter(monkeypatch, tty=False)
    ui.status("[red]provider[/red]\x1b[2J", "missing", "unsafe\rrewrite")
    assert "[red]provider[/red]" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()
    assert "\r" not in stream.getvalue()


def test_recommendations_explain_optional_providers(monkeypatch):
    ui, stream = presenter(monkeypatch, tty=False)
    ui.recommendations(
        [
            {
                "capability_id": "code.symbols",
                "status": "missing",
                "candidate_providers": ["intelligence.serena"],
                "reason": "Semantic navigation",
                "optional": True,
            }
        ]
    )
    output = stream.getvalue()
    assert "RECOMMENDED" in output
    assert "intelligence.serena" in output
    assert "Semantic navigation" in output


@pytest.mark.parametrize(
    "result,expected", [("failed", "FAILED"), ("blocked", "BLOCKED"), ("success", "APPLIED")]
)
def test_bootstrap_summary_uses_action_results_not_ok_stage(monkeypatch, result, expected):
    ui, stream = presenter(monkeypatch, tty=False)
    ui.bootstrap(
        {
            "stages": [{"stage": "apply", "status": "ok", "summary": "Operation returned"}],
            "providers": [],
            "recommendations": [],
            "plan": {"actions": []},
            "apply": {
                "mutated": result == "success",
                "actions": [{"action_id": "serena", "result": {"status": result}}],
            },
            "certification": {
                "results": [
                    {"provider_id": "serena", "certified": False, "reason": "installed != healthy"}
                ]
            },
            "mutation_free": result != "success",
        }
    )
    output = stream.getvalue()
    assert expected in output
    assert "installed != healthy" in output
    assert "VERDICT READY" not in output


def test_doctor_keeps_missing_capabilities_visible(monkeypatch):
    ui, stream = presenter(monkeypatch, tty=False)
    ui.doctor(
        {
            "capabilities": [
                {
                    "capability_id": "code.symbols",
                    "status": "covered",
                    "selected_provider_id": "native",
                    "health": "healthy",
                },
                {
                    "capability_id": "memory.search",
                    "status": "missing",
                    "selected_provider_id": None,
                    "health": None,
                },
            ]
        }
    )
    assert "memory.search" in stream.getvalue()
    assert "MISSING" in stream.getvalue()
    assert "native" in stream.getvalue()


def test_setup_json_stays_machine_readable(monkeypatch, capsys):
    from verdict.cli import cmd_setup

    monkeypatch.setenv("FORCE_COLOR", "1")
    cmd_setup(dry_run=True, output_json=True)
    output = capsys.readouterr().out
    assert json.loads(output)["mutation_free"] is True
    assert "\x1b" not in output
