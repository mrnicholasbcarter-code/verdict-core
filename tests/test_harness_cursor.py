"""Unit tests for Cursor Verdict-managed harness switching.

CLI surface:
  verdict harness cursor {discover,enable,disable,status,certify}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from verdict import cli
from verdict.harness_cursor import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_ENV,
    PROVIDER_FILE,
    HarnessCursorError,
    certify,
    disable,
    discover,
    enable,
    format_certify,
    format_status,
    resolve_paths,
    status,
)

SECRET = "super-secret-cursor-token-do-not-print"
ORIGINAL_PROVIDER = """\
{
  "provider": "other",
  "base_url": "https://example.invalid/v1"
}
"""


def _cursor_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".cursor"
    home.mkdir(parents=True)
    return home


def _healthy(_base_url: str) -> bool:
    return True


def _unhealthy(_base_url: str) -> bool:
    return False


def test_default_base_url_is_verdict_not_omniroute() -> None:
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000/v1"
    assert "20128" not in DEFAULT_BASE_URL


def test_enable_prefers_verdict_managed_provider(tmp_path: Path) -> None:
    cursor_home = _cursor_home(tmp_path)
    provider = cursor_home / PROVIDER_FILE
    provider.write_text(ORIGINAL_PROVIDER, encoding="utf-8")

    result = enable(cursor_home=cursor_home, health_check=_healthy, wrapper=False)
    assert result.created_backup is True
    assert result.integration in {"verdict-managed", "openai-compatible", "wrapper"}
    assert "8000" in result.base_url
    assert "20128" not in result.base_url

    data = json.loads(provider.read_text(encoding="utf-8"))
    assert data["provider"] == "verdict"
    assert data["enabled"] is True
    assert data["base_url"] == DEFAULT_BASE_URL
    assert data["token_env"] == DEFAULT_TOKEN_ENV
    assert "api_key" not in data
    assert SECRET not in provider.read_text(encoding="utf-8")

    env_text = (cursor_home / "verdict-openai.env").read_text(encoding="utf-8")
    assert DEFAULT_BASE_URL in env_text
    assert SECRET not in env_text


def test_enable_writes_settings_when_present(tmp_path: Path) -> None:
    cursor_home = _cursor_home(tmp_path)
    settings = tmp_path / "home" / ".config" / "Cursor" / "User" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"editor.fontSize": 14}\n', encoding="utf-8")

    result = enable(
        cursor_home=cursor_home, settings_path=settings, health_check=_healthy, wrapper=False
    )
    assert result.settings_path == settings
    assert result.integration == "openai-compatible"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["openai.baseUrl"] == DEFAULT_BASE_URL
    assert data["editor.fontSize"] == 14
    assert SECRET not in settings.read_text(encoding="utf-8")


def test_enable_wrapper_last(tmp_path: Path) -> None:
    cursor_home = _cursor_home(tmp_path)
    result = enable(
        cursor_home=cursor_home,
        health_check=_healthy,
        wrapper=True,
        settings_path=tmp_path / "missing-settings.json",
    )
    assert result.wrapper_path is not None
    assert result.wrapper_path.is_file()
    script = result.wrapper_path.read_text(encoding="utf-8")
    assert "8000" in script
    assert "20128" not in script
    assert SECRET not in script


def test_enable_refuses_omniroute_and_unhealthy(tmp_path: Path) -> None:
    cursor_home = _cursor_home(tmp_path)
    with pytest.raises(HarnessCursorError, match=r"OmniRoute|20128"):
        enable(
            cursor_home=cursor_home,
            base_url="http://127.0.0.1:20128/v1",
            health_check=_healthy,
            force=False,
        )
    with pytest.raises(HarnessCursorError, match="health"):
        enable(cursor_home=cursor_home, health_check=_unhealthy, force=False)


def test_disable_restores_backup(tmp_path: Path) -> None:
    cursor_home = _cursor_home(tmp_path)
    provider = cursor_home / PROVIDER_FILE
    provider.write_text(ORIGINAL_PROVIDER, encoding="utf-8")
    enable(cursor_home=cursor_home, health_check=_healthy, wrapper=False)
    disable(cursor_home=cursor_home)
    assert provider.read_text(encoding="utf-8") == ORIGINAL_PROVIDER
    assert not resolve_paths(cursor_home=cursor_home).provider_backup.exists()


def test_discover_status_certify_redact_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, SECRET)
    cursor_home = _cursor_home(tmp_path)
    enable(cursor_home=cursor_home, health_check=_healthy, wrapper=False)

    found = discover(cursor_home=cursor_home, which=lambda _name: "/tmp/cursor")
    assert found.pointing_at_verdict is True
    assert SECRET not in str(found)

    report = status(cursor_home=cursor_home)
    assert report.enabled is True
    assert SECRET not in format_status(report)

    cert = certify(
        cursor_home=cursor_home,
        health_check=_healthy,
        which=lambda _name: "/tmp/cursor",
    )
    assert cert.overall == "partial"
    assert any("secrets" in item for item in cert.needs_owner)
    assert SECRET not in format_certify(cert)


def test_cli_help_lists_cursor_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "cursor", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("discover", "enable", "disable", "status", "certify"):
        assert name in out
