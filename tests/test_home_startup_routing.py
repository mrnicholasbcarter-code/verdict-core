"""Isolated launch routing; the only health checker is the canonical doctor action."""
import io

import pytest
from rich.console import Console

from verdict.home import run_home
from verdict.home_startup_render import route_startup
from verdict.terminal_ui import TerminalUI


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("VERDICT_SKIP_SETUP", raising=False)
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    return TerminalUI(console), tmp_path


def complete(monkeypatch, root):
    home = root / ".verdict"
    home.mkdir()
    (home / "setup-complete").write_text("reviewed")
    config = root / "config" / "verdict"
    config.mkdir(parents=True)
    (config / "verdict.yaml").write_text("gateway_url: http://fixture")
    monkeypatch.setattr("verdict.home_startup_render.resolve_api_key", lambda *a: "fixture-key")
    return home


def test_first_run_non_tty_does_not_check_or_read(isolated):
    ui, root = isolated
    assert run_home(console=ui.console, probe=False) == 0
    text = ui.console.file.getvalue()
    assert "First-run setup" in text and "--skip-setup" in text
    assert not (root / ".verdict" / "setup-complete").exists()


@pytest.mark.parametrize("env", [False, True])
def test_skip_bypasses_setup_and_preflight(isolated, monkeypatch, env):
    ui, _root = isolated
    if env:
        monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    assert run_home(console=ui.console, probe=False, skip_setup=not env) == 0
    assert "COMMANDS" in ui.console.file.getvalue()
    assert "First-run setup" not in ui.console.file.getvalue()


def test_completed_setup_runs_canonical_preflight(isolated, monkeypatch):
    ui, root = isolated
    home = complete(monkeypatch, root)
    calls = []

    def run(name, params=None):
        calls.append((name, params))
        return True, {"issues": [], "warnings": ["fixture warning"]}

    assert route_startup(ui, home=home, gateway="http://fixture", run=run) is None
    assert calls == [("doctor", {"fix": False, "preflight_timeout": 2.0})]
    assert "Startup warning: fixture warning" in ui.console.file.getvalue()


def test_startup_errors_non_tty_render_report_and_exit(isolated, monkeypatch):
    ui, root = isolated
    home = complete(monkeypatch, root)
    report = {"issues": ["fixture failed"], "sections": [{"label": "Gateway", "state": "failed"}],
              "capability_bootstrap": {"capabilities": []}}
    assert route_startup(ui, home=home, gateway="http://fixture", run=lambda *a: (False, report)) == 1
    output = ui.console.file.getvalue()
    assert "FAILED" in output and "Repair:" in output
    assert "capability_bootstrap" not in output


def test_startup_unknown_goes_home_with_banner(isolated, monkeypatch):
    ui, root = isolated
    home = complete(monkeypatch, root)
    monkeypatch.setattr("verdict.home_startup_render._preflight", lambda run: None)
    assert route_startup(ui, home=home, gateway="http://fixture", run=lambda *a: None) is None
    assert "Startup health unknown" in ui.console.file.getvalue()


def test_shared_setup_writer_preserves_and_backs_up(isolated):
    import os
    import yaml
    from verdict.setup_config import save_setup_config

    _ui, _root = isolated
    path = save_setup_config({"primary_model": "fixture"})
    old = path.read_bytes()
    save_setup_config({"gateway_url": "http://fixture"})
    assert yaml.safe_load(path.read_text())["primary_model"] == "fixture"
    backups = list(path.parent.glob("verdict.yaml.backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == old
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(backups[0]).st_mode & 0o777 == 0o600
