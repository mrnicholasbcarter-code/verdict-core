"""Unit tests for OpenCode Verdict-managed harness switching.

CLI surface:
  verdict harness opencode {discover,enable,disable,status,certify}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from verdict import cli
from verdict.harness_opencode import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TOKEN_ENV,
    PROVIDER_ID,
    HarnessOpenCodeError,
    certify,
    disable,
    discover,
    enable,
    format_certify,
    format_status,
    resolve_paths,
    status,
)

SECRET = "super-secret-opencode-token-do-not-print"
ORIGINAL_CONFIG = """\
{
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama",
      "options": {"baseURL": "http://127.0.0.1:11434/v1"},
      "models": {"llama": {"name": "Llama"}}
    }
  },
  "model": "ollama/llama"
}
"""


def _opencode_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".config" / "opencode"
    home.mkdir(parents=True)
    return home


def _healthy(_base_url: str) -> bool:
    return True


def _unhealthy(_base_url: str) -> bool:
    return False


def test_default_base_url_is_verdict_not_omniroute() -> None:
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000/v1"
    assert "20128" not in DEFAULT_BASE_URL
    assert DEFAULT_MODEL == "verdict/default"


def test_enable_upserts_provider_and_backup(tmp_path: Path) -> None:
    opencode_home = _opencode_home(tmp_path)
    config = opencode_home / "opencode.json"
    config.write_text(ORIGINAL_CONFIG, encoding="utf-8")

    result = enable(opencode_home=opencode_home, health_check=_healthy)
    assert result.created_backup is True
    assert result.backup_path.read_text(encoding="utf-8") == ORIGINAL_CONFIG
    assert result.model == DEFAULT_MODEL

    data = json.loads(config.read_text(encoding="utf-8"))
    assert "ollama" in data["provider"]
    verdict = data["provider"][PROVIDER_ID]
    assert verdict["options"]["baseURL"] == DEFAULT_BASE_URL
    assert "openai-compatible" in verdict["npm"]
    assert verdict["env"] == [DEFAULT_TOKEN_ENV]
    assert data["model"] == DEFAULT_MODEL
    assert SECRET not in config.read_text(encoding="utf-8")
    assert "apiKey" not in verdict
    assert "20128" not in verdict["options"]["baseURL"]


def test_enable_refuses_omniroute_and_unhealthy(tmp_path: Path) -> None:
    opencode_home = _opencode_home(tmp_path)
    (opencode_home / "opencode.json").write_text(ORIGINAL_CONFIG, encoding="utf-8")
    with pytest.raises(HarnessOpenCodeError, match=r"OmniRoute|20128"):
        enable(
            opencode_home=opencode_home,
            base_url="http://127.0.0.1:20128/v1",
            health_check=_healthy,
            force=False,
        )
    with pytest.raises(HarnessOpenCodeError, match="health"):
        enable(opencode_home=opencode_home, health_check=_unhealthy, force=False)


def test_disable_restores_backup_exactly(tmp_path: Path) -> None:
    opencode_home = _opencode_home(tmp_path)
    config = opencode_home / "opencode.json"
    config.write_text(ORIGINAL_CONFIG, encoding="utf-8")
    enable(opencode_home=opencode_home, health_check=_healthy)
    disable(opencode_home=opencode_home)
    assert config.read_text(encoding="utf-8") == ORIGINAL_CONFIG
    assert not resolve_paths(opencode_home=opencode_home).backup.exists()


def test_discover_status_certify_graceful_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, SECRET)
    opencode_home = _opencode_home(tmp_path)
    (opencode_home / "opencode.json").write_text(ORIGINAL_CONFIG, encoding="utf-8")
    enable(opencode_home=opencode_home, health_check=_healthy)

    missing = discover(opencode_home=opencode_home, which=lambda _name: None)
    assert missing.installed is False
    assert SECRET not in str(missing)

    found = discover(
        opencode_home=opencode_home,
        which=lambda name: "/tmp/opencode" if name == "opencode" else None,
    )
    assert found.installed is True
    assert found.pointing_at_verdict is True

    report = status(opencode_home=opencode_home)
    assert report.enabled is True
    assert report.model == DEFAULT_MODEL
    assert SECRET not in format_status(report)

    not_installed = certify(
        opencode_home=opencode_home, health_check=_healthy, which=lambda _name: None
    )
    assert not_installed.overall == "not-installed"

    go_binary = certify(
        opencode_home=opencode_home,
        health_check=_healthy,
        which=lambda name: "/tmp/opencode-go" if name == "opencode-go" else None,
    )
    assert go_binary.overall == "partial"
    assert any("secrets" in item for item in go_binary.needs_owner)
    assert SECRET not in format_certify(go_binary)


def test_cli_help_lists_opencode_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "opencode", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("discover", "enable", "disable", "status", "certify"):
        assert name in out
    assert SECRET not in out
