"""Unit tests for Hermes Verdict-managed harness switching.

CLI surface:
  verdict harness hermes enable [--base-url ...] [--token-env ...] [--model ...] [--force]
  verdict harness hermes disable
  verdict harness hermes status
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from verdict import cli
from verdict.harness_hermes import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_ENV,
    PROVIDER_NAME,
    HarnessHermesError,
    disable,
    enable,
    format_status,
    resolve_paths,
    status,
)

SECRET = "super-secret-hermes-token-do-not-print"
ORIGINAL_YAML = """\
model:
  default: thinkingmachines/inkling:free
  provider: openrouter
  base_url: https://openrouter.ai/api/v1
  api_mode: chat_completions
custom_providers:
  - name: OmniRoute
    base_url: http://localhost:20128/v1
    key_env: HERMES_CUSTOM_LOCALHOST_20128_API_KEY
    model: claude/kc/hy3:free
agent:
  max_turns: 150
"""


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    return home


def _healthy(_base_url: str) -> bool:
    return True


def _unhealthy(_base_url: str) -> bool:
    return False


def test_default_base_url_is_verdict_not_omniroute() -> None:
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000/v1"
    assert "8000" in DEFAULT_BASE_URL
    assert "20128" not in DEFAULT_BASE_URL
    assert DEFAULT_TOKEN_ENV == "LLMGATE_AUTH_TOKEN"
    assert PROVIDER_NAME == "Verdict"


def test_enable_writes_provider_and_backup(tmp_path: Path) -> None:
    hermes_home = _hermes_home(tmp_path)
    config = hermes_home / "config.yaml"
    config.write_text(ORIGINAL_YAML, encoding="utf-8")

    result = enable(
        hermes_home=hermes_home, force=True, health_check=_healthy, model="verdict/default"
    )
    assert result.created_backup is True
    assert result.backup_path.is_file()
    assert result.backup_path.read_text(encoding="utf-8") == ORIGINAL_YAML

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["model"]["provider"] == PROVIDER_NAME
    assert data["model"]["base_url"] == DEFAULT_BASE_URL
    assert data["model"]["default"] == "verdict/default"
    providers = data["custom_providers"]
    verdict = next(p for p in providers if p["name"] == PROVIDER_NAME)
    assert verdict["base_url"] == DEFAULT_BASE_URL
    assert verdict["key_env"] == DEFAULT_TOKEN_ENV
    # Preserve unrelated OmniRoute entry
    assert any(p.get("name") == "OmniRoute" for p in providers)
    assert any(p.get("base_url") == "http://localhost:20128/v1" for p in providers)


def test_enable_refuses_unhealthy_without_force(tmp_path: Path) -> None:
    hermes_home = _hermes_home(tmp_path)
    (hermes_home / "config.yaml").write_text(ORIGINAL_YAML, encoding="utf-8")
    with pytest.raises(HarnessHermesError, match="health check failed"):
        enable(hermes_home=hermes_home, health_check=_unhealthy, force=False)


def test_disable_restores_backup_exactly(tmp_path: Path) -> None:
    hermes_home = _hermes_home(tmp_path)
    config = hermes_home / "config.yaml"
    config.write_text(ORIGINAL_YAML, encoding="utf-8")
    enable(hermes_home=hermes_home, force=True, health_check=_healthy, model="x")
    assert "Verdict" in config.read_text(encoding="utf-8")
    disable(hermes_home=hermes_home)
    assert config.read_text(encoding="utf-8") == ORIGINAL_YAML
    assert not resolve_paths(hermes_home=hermes_home).backup.exists()


def test_status_never_prints_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermes_home = _hermes_home(tmp_path)
    (hermes_home / "config.yaml").write_text(ORIGINAL_YAML, encoding="utf-8")
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, SECRET)
    enable(hermes_home=hermes_home, force=True, health_check=_healthy, model="x")
    report = status(hermes_home=hermes_home)
    rendered = format_status(report)
    assert SECRET not in rendered
    assert report.enabled is True
    assert report.token_env_set is True
    assert report.provider == PROVIDER_NAME


def test_cli_help_lists_hermes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "hermes", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "enable" in out
    assert "disable" in out
    assert "status" in out
    assert SECRET not in out
