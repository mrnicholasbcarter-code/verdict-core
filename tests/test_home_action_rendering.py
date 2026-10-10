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


def test_setup_confirms_backup_and_completion(home_ui, monkeypatch, tmp_path):
    from verdict.setup_config import save_setup_config
    save_setup_config({"primary_model": "fixture"})
    monkeypatch.setattr("verdict.home.probe_gateway", lambda *a, **k: (True, 2, None))
    # gateway + check; three choices/confirms; five catalog reviews; three manual;
    # warm-cache choice/confirm; final save.
    answers = ["http://fixture", "y", "keep", "n", "keep", "n", "keep", "n",
               *(["n"] * 8), "off", "n", "y"]
    output = run_command(home_ui, "/setup", answers)
    assert "saved" in output.lower()
    assert (tmp_path / ".verdict" / "setup-complete").is_file()
    assert list((tmp_path / "config" / "verdict").glob("verdict.yaml.backup-*"))
    assert "plan_digest" not in output


def test_setup_dependencies_show_manual_and_skips(home_ui):
    output = run_command(home_ui, "/setup")
    for term in ("gateway.omniroute", "harness.prime", "adapter.codebase_memory",
                 "adapter.serena_lsp", "adapter.context7", "context-mode",
                 "open-code-review", "ai-memory"):
        assert term in output
    assert "Install:" in output and "Version" in output
    assert "manual" in output and "skipped" in output
