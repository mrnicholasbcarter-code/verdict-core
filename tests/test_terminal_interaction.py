import io
import sys

from rich.console import Console

from verdict.capability_bootstrap import BootstrapMode, BootstrapScope, UnifiedBootstrapPlan
from verdict.terminal_ui import TerminalUI


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


def test_no_color_still_accepts_explicit_consent_and_details(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setattr(sys, "stdin", TerminalInput("details\ny\n"))
    stream = io.StringIO()
    ui = TerminalUI(Console(file=stream, force_terminal=True))
    plan = UnifiedBootstrapPlan((), True, BootstrapScope.ALL, BootstrapMode.PLAN)
    assert ui.confirm_plan(plan) is True
    assert "No actions proposed" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_empty_consent_answer_declines(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n"))
    ui = TerminalUI(Console(file=io.StringIO(), force_terminal=True))
    plan = UnifiedBootstrapPlan((), True, BootstrapScope.ALL, BootstrapMode.PLAN)
    assert ui.confirm_plan(plan) is False


def test_no_animation_keeps_tty_colors_but_no_cursor_controls(monkeypatch):
    for key in ("CI", "NO_COLOR", "TERM", "VERDICT_PLAIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VERDICT_NO_ANIMATION", "1")
    stream = io.StringIO()
    ui = TerminalUI(Console(file=stream, force_terminal=True))
    with ui.task("Checking"):
        pass
    assert "RUNNING" in stream.getvalue()
    assert "\x1b[?25" not in stream.getvalue()


def test_plain_plan_golden_preserves_primary_information(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    ui = TerminalUI(Console(file=stream, width=80))
    ui.plan(
        {
            "actions": [
                {
                    "kind": "install_provider",
                    "description": "Install Serena",
                    "reason": "Semantic navigation",
                },
                {"kind": "preserve_config", "description": "Keep existing MCP configuration"},
            ]
        }
    )
    assert (
        stream.getvalue()
        == "\n  Plan\nInstall · Install Serena\n  Semantic navigation\nPreserve · Keep existing MCP configuration\n"
    )
