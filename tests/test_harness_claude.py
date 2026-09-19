"""Unit tests for Claude Code Verdict-managed harness switching.

CLI surface:
  verdict harness claude {discover,enable,disable,status,certify}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from verdict import cli
from verdict.harness_claude import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_ENV,
    HarnessClaudeError,
    certify,
    disable,
    discover,
    enable,
    format_certify,
    format_status,
    resolve_paths,
    status,
)

SECRET = "super-secret-claude-token-do-not-print"
ORIGINAL_SETTINGS = """\
{
  "env": {
    "SOME_OTHER": "keep-me"
  },
  "hooks": {}
}
"""


def _claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    return home


def _healthy(_base_url: str) -> bool:
    return True


def _unhealthy(_base_url: str) -> bool:
    return False


def test_default_base_url_is_verdict_not_omniroute() -> None:
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000/v1"
    assert "20128" not in DEFAULT_BASE_URL
    assert DEFAULT_TOKEN_ENV == "LLMGATE_AUTH_TOKEN"


def test_enable_writes_openai_env_gate_and_backup(tmp_path: Path) -> None:
    claude_home = _claude_home(tmp_path)
    config = claude_home / "settings.json"
    config.write_text(ORIGINAL_SETTINGS, encoding="utf-8")

    result = enable(claude_home=claude_home, health_check=_healthy)
    assert result.created_backup is True
    assert result.backup_path.read_text(encoding="utf-8") == ORIGINAL_SETTINGS

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["env"]["OPENAI_BASE_URL"] == DEFAULT_BASE_URL
    assert data["env"]["SOME_OTHER"] == "keep-me"
    assert data["env"]["VERDICT_HARNESS"] == "claude"
    assert "20128" not in data["env"]["OPENAI_BASE_URL"]
    assert "ANTHROPIC_BASE_URL" not in data["env"]
    assert SECRET not in config.read_text(encoding="utf-8")
    session = data["hooks"]["SessionStart"]
    assert any(
        any(
            h.get("command") == "verdict" and h.get("args") == ["hook", "claude-gate"]
            for h in entry.get("hooks", [])
        )
        for entry in session
    )


def test_enable_refuses_omniroute_and_unhealthy(tmp_path: Path) -> None:
    claude_home = _claude_home(tmp_path)
    (claude_home / "settings.json").write_text(ORIGINAL_SETTINGS, encoding="utf-8")
    with pytest.raises(HarnessClaudeError, match=r"OmniRoute|20128"):
        enable(
            claude_home=claude_home,
            base_url="http://127.0.0.1:20128/v1",
            health_check=_healthy,
            force=False,
        )
    with pytest.raises(HarnessClaudeError, match="health"):
        enable(claude_home=claude_home, health_check=_unhealthy, force=False)


def test_disable_restores_backup_exactly(tmp_path: Path) -> None:
    claude_home = _claude_home(tmp_path)
    config = claude_home / "settings.json"
    config.write_text(ORIGINAL_SETTINGS, encoding="utf-8")
    enable(claude_home=claude_home, health_check=_healthy)
    disable(claude_home=claude_home)
    assert config.read_text(encoding="utf-8") == ORIGINAL_SETTINGS
    assert not resolve_paths(claude_home=claude_home).backup.exists()


def test_discover_and_status_never_print_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, SECRET)
    claude_home = _claude_home(tmp_path)
    (claude_home / "settings.json").write_text(ORIGINAL_SETTINGS, encoding="utf-8")
    enable(claude_home=claude_home, health_check=_healthy)

    found = discover(claude_home=claude_home, which=lambda _name: "/tmp/claude")
    assert found.installed is True
    assert found.pointing_at_verdict is True
    assert found.pointing_at_omniroute is False
    assert SECRET not in str(found)

    report = status(claude_home=claude_home)
    rendered = format_status(report)
    assert SECRET not in rendered
    assert report.enabled is True
    assert report.token_env_set is True
    assert report.gate_hook_present is True


def test_certify_is_partial_without_live_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    claude_home = _claude_home(tmp_path)
    enable(claude_home=claude_home, health_check=_healthy)
    report = certify(
        claude_home=claude_home,
        health_check=_healthy,
        which=lambda _name: "/tmp/claude",
    )
    assert report.overall == "partial"
    assert report.facets["hooks"] == "supported"
    assert report.facets["model_selection"] == "partial"
    assert any("BOD-102" in note or "Messages" in note for note in report.notes)
    assert any("secrets" in item for item in report.needs_owner)
    assert SECRET not in format_certify(report)


def test_cli_help_lists_claude_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "claude", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("discover", "enable", "disable", "status", "certify"):
        assert name in out
    assert SECRET not in out
