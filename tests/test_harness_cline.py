"""Unit tests for Cline Verdict-managed harness switching.

CLI surface:
  verdict harness cline {discover,enable,disable,status,certify}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from verdict import cli
from verdict.harness_cline import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_ENV,
    PROVIDER_FILE,
    HarnessClineError,
    certify,
    disable,
    discover,
    enable,
    format_certify,
    format_discover,
    format_status,
    resolve_paths,
    status,
)

SECRET = "super-secret-cline-token-do-not-print"
ORIGINAL_PROVIDER = """\
{
  "provider": "other",
  "base_url": "https://example.invalid/v1"
}
"""


def _cline_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".cline"
    home.mkdir(parents=True)
    return home


def _healthy(_base_url: str) -> bool:
    return True


def _unhealthy(_base_url: str) -> bool:
    return False


def test_default_base_url_is_verdict_not_omniroute() -> None:
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000/v1"
    assert "20128" not in DEFAULT_BASE_URL


def test_enable_cli_path_writes_providers_json(tmp_path: Path) -> None:
    cline_home = _cline_home(tmp_path)
    provider = cline_home / PROVIDER_FILE
    provider.write_text(ORIGINAL_PROVIDER, encoding="utf-8")

    result = enable(cline_home=cline_home, health_check=_healthy, which=lambda _name: "/tmp/cline")
    assert result.created_backup is True
    assert result.integration == "cline-cli"
    assert result.providers_json_path is not None
    assert "8000" in result.base_url
    assert "20128" not in result.base_url

    data = json.loads(provider.read_text(encoding="utf-8"))
    assert data["provider"] == "verdict"
    assert data["enabled"] is True
    assert data["base_url"] == DEFAULT_BASE_URL
    assert data["token_env"] == DEFAULT_TOKEN_ENV
    assert "api_key" not in data
    assert SECRET not in provider.read_text(encoding="utf-8")

    providers = json.loads(result.providers_json_path.read_text(encoding="utf-8"))
    assert providers["verdict"]["baseUrl"] == DEFAULT_BASE_URL
    assert SECRET not in result.providers_json_path.read_text(encoding="utf-8")

    env_text = (cline_home / "verdict-openai.env").read_text(encoding="utf-8")
    assert DEFAULT_BASE_URL in env_text
    assert SECRET not in env_text


def test_enable_ide_only_writes_settings_and_ui_steps(tmp_path: Path) -> None:
    # Fresh home (not pre-created) + no cline binary → IDE settings path.
    cline_home = tmp_path / "home" / ".cline"
    settings = tmp_path / "home" / ".config" / "Code" / "User" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"editor.fontSize": 14}\n', encoding="utf-8")

    result = enable(
        cline_home=cline_home,
        settings_path=settings,
        health_check=_healthy,
        which=lambda _name: None,
    )
    assert result.integration == "ide-settings"
    assert result.settings_path == settings
    assert result.ui_steps
    assert any("OpenAI Compatible" in step for step in result.ui_steps)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["cline.openAiBaseUrl"] == DEFAULT_BASE_URL
    assert data["cline.apiProvider"] == "openai-compatible"
    assert data["editor.fontSize"] == 14
    assert "cline.openAiApiKey" not in data
    assert SECRET not in settings.read_text(encoding="utf-8")


def test_enable_refuses_omniroute_and_unhealthy(tmp_path: Path) -> None:
    cline_home = _cline_home(tmp_path)
    with pytest.raises(HarnessClineError, match=r"OmniRoute|20128"):
        enable(
            cline_home=cline_home,
            base_url="http://127.0.0.1:20128/v1",
            health_check=_healthy,
            force=False,
            which=lambda _name: None,
        )
    with pytest.raises(HarnessClineError, match="health"):
        enable(
            cline_home=cline_home, health_check=_unhealthy, force=False, which=lambda _name: None
        )


def test_disable_restores_backup(tmp_path: Path) -> None:
    cline_home = _cline_home(tmp_path)
    provider = cline_home / PROVIDER_FILE
    provider.write_text(ORIGINAL_PROVIDER, encoding="utf-8")
    enable(cline_home=cline_home, health_check=_healthy, which=lambda _name: "/tmp/cline")
    disable(cline_home=cline_home)
    assert provider.read_text(encoding="utf-8") == ORIGINAL_PROVIDER
    assert not resolve_paths(cline_home=cline_home).provider_backup.exists()


def test_discover_graceful_not_installed(tmp_path: Path) -> None:
    cline_home = _cline_home(tmp_path)
    found = discover(cline_home=cline_home, which=lambda _name: None)
    assert found.installed is False
    assert found.binary_path is None
    text = format_discover(found)
    assert "installed: no" in text
    assert SECRET not in text


def test_discover_status_certify_redact_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, SECRET)
    cline_home = _cline_home(tmp_path)
    enable(cline_home=cline_home, health_check=_healthy, which=lambda _name: "/tmp/cline")

    found = discover(cline_home=cline_home, which=lambda _name: "/tmp/cline")
    assert found.pointing_at_verdict is True
    assert SECRET not in str(found)

    report = status(cline_home=cline_home, which=lambda _name: "/tmp/cline")
    assert report.enabled is True
    assert SECRET not in format_status(report)

    cert = certify(cline_home=cline_home, health_check=_healthy, which=lambda _name: "/tmp/cline")
    assert cert.overall == "partial"
    assert any("secrets" in item for item in cert.needs_owner)
    assert any("OmniRoute" in note for note in cert.notes)
    assert SECRET not in format_certify(cert)


def test_certify_not_installed_without_config(tmp_path: Path) -> None:
    cline_home = tmp_path / "missing-cline-home"
    cert = certify(
        cline_home=cline_home, health_check=_healthy, which=lambda _name: None, force=True
    )
    assert cert.overall == "unsupported"
    assert any("not found" in note for note in cert.notes)


def test_cli_help_lists_cline_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "cline", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("discover", "enable", "disable", "status", "certify"):
        assert name in out
