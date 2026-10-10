"""Home commands use real actions with isolated collectors, never live providers."""
import io

import pytest
from rich.console import Console

from verdict.doctor_diagnostics import DoctorDiagnostics
from verdict.home import HomeState, _run_command
from verdict.terminal_ui import TerminalUI


@pytest.fixture
def home_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    return TerminalUI(console)


def run_command(ui, command, answers=()):
    lines = iter(answers)
    _run_command(command, tui=ui, state=HomeState(gateway="http://localhost:20128"),
                 line_reader=lambda: next(lines, None))
    return ui.console.file.getvalue()


def test_home_doctor_renders_failed_report(home_ui, monkeypatch):
    diag = DoctorDiagnostics()
    diag.capability_report = {"capabilities": [{"capability_id": "gateway.execute", "status": "missing"}]}
    diag.sections = [("Gateway", "failed", "not reachable"), ("Harness", "ok", "installed")]
    diag.issues = ["Gateway unreachable"]
    monkeypatch.setattr("verdict.actions.helpers.collect_doctor_diagnostics", lambda *a, **k: diag)
    output = run_command(home_ui, "/doctor")
    assert "CAPABILITIES" in output
    assert "FAILED" in output and "OK" in output
    assert "Repair:" in output
    assert "capability_bootstrap" not in output
    assert "{\"" not in output


def test_home_generic_nested_values_are_readable(home_ui, monkeypatch):
    monkeypatch.setattr("verdict.home.run_palette_action", lambda *a, **k: (
        True, {"config": {"providers": [{"name": "fixture", "enabled": True}]}, "plan_digest": "hidden"}))
    output = run_command(home_ui, "/config")
    assert "Config" in output and "fixture" in output
    assert "Next step" in output
    assert "plan_digest" not in output
    assert "{'" not in output and "[{" not in output


def test_home_doctor_json_is_explicit(home_ui, monkeypatch):
    diag = DoctorDiagnostics()
    monkeypatch.setattr("verdict.actions.helpers.collect_doctor_diagnostics", lambda *a, **k: diag)
    output = run_command(home_ui, "/doctor --json")
    import json
    assert json.loads(output)["status"] == "ok"


def test_home_setup_plain_defaults_do_not_apply(home_ui):
    output = run_command(home_ui, "/setup")
    for label in ("Gateway", "Harness", "Memory", "Documentation", "Warm-cache"):
        assert label in output
    assert "Current:" in output and "Recommended:" in output
    assert "Confirm" in output and "Summary" in output
    assert "No changes" in output
    assert "plan_digest" not in output
