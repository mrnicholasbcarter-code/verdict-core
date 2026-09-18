"""Unit tests for Architect-locked Codex harness switching.

CLI surface (do not rename):
  verdict harness codex enable [--base-url ...] [--token-env ...] [--force]
  verdict harness codex disable
  verdict harness codex status
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from verdict import cli
from verdict.harness_codex import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_ENV,
    HarnessCodexError,
    disable,
    enable,
    format_status,
    health_urls,
    resolve_paths,
    status,
)

SECRET = "super-secret-token-value-do-not-print"
ORIGINAL_TOML = """# keep this comment
model = "cx/gpt-5.6-luna-xhigh"
model_provider = "omniroute"
approval_policy = "on-request"

[model_providers.omniroute]
base_url = "http://127.0.0.1:20128/v1"
wire_api = "responses"

[projects."/home/nick/dev"]
trust_level = "trusted"
"""


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _codex_home(tmp_path: Path) -> Path:
    return _home(tmp_path) / ".codex"


def _healthy(_base_url: str) -> bool:
    return True


def _unhealthy(_base_url: str) -> bool:
    return False


def test_default_base_url_is_verdict_not_omniroute() -> None:
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000/v1"
    assert "8000" in DEFAULT_BASE_URL
    assert "20128" not in DEFAULT_BASE_URL
    assert DEFAULT_TOKEN_ENV == "LLMGATE_AUTH_TOKEN"


def test_health_urls_derive_from_verdict_base() -> None:
    urls = health_urls("http://127.0.0.1:8000/v1")
    assert "http://127.0.0.1:8000/health" in urls
    assert "http://127.0.0.1:8000/v1/models" in urls
    assert all("20128" not in url for url in urls)


def test_enable_writes_provider_and_backup(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    config.write_text(ORIGINAL_TOML, encoding="utf-8")

    result = enable(codex_home=codex_home, health_check=_healthy)

    paths = resolve_paths(codex_home=codex_home)
    assert result.config_path == paths.config
    assert paths.backup.is_file()
    assert paths.backup.read_text(encoding="utf-8") == ORIGINAL_TOML

    written = config.read_text(encoding="utf-8")
    assert 'model_provider = "verdict"' in written
    assert 'base_url = "http://127.0.0.1:8000/v1"' in written
    assert 'env_key = "LLMGATE_AUTH_TOKEN"' in written
    assert 'wire_api = "responses"' in written
    assert "requires_openai_auth = false" in written
    assert "[model_providers.verdict]" in written
    assert "20128" not in written.split("[model_providers.verdict]")[-1].split("[")[0]
    assert 'model = "cx/gpt-5.6-luna-xhigh"' in written
    assert 'approval_policy = "on-request"' in written
    assert "[model_providers.omniroute]" in written
    assert 'trust_level = "trusted"' in written


def test_enable_refuses_unhealthy_without_force(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    config.write_text(ORIGINAL_TOML, encoding="utf-8")
    paths = resolve_paths(codex_home=codex_home)

    with pytest.raises(HarnessCodexError, match="health"):
        enable(codex_home=codex_home, health_check=_unhealthy)

    assert config.read_text(encoding="utf-8") == ORIGINAL_TOML
    assert not paths.backup.exists()


def test_enable_force_skips_health_and_writes(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    enable(codex_home=codex_home, force=True, health_check=_unhealthy)
    written = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "verdict"' in written
    assert "[model_providers.verdict]" in written


def test_enable_custom_base_url_and_token_env(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    enable(
        codex_home=codex_home,
        base_url="http://127.0.0.1:8000/v1",
        token_env="VERDICT_SERVE_TOKEN",
        health_check=_healthy,
    )
    written = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'env_key = "VERDICT_SERVE_TOKEN"' in written
    assert 'base_url = "http://127.0.0.1:8000/v1"' in written


def test_enable_does_not_overwrite_existing_backup(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    config.write_text(ORIGINAL_TOML, encoding="utf-8")
    enable(codex_home=codex_home, health_check=_healthy)
    paths = resolve_paths(codex_home=codex_home)
    original_backup = paths.backup.read_bytes()
    enable(
        codex_home=codex_home,
        base_url="http://127.0.0.1:8000/v1",
        token_env="OTHER_TOKEN",
        health_check=_healthy,
    )
    assert paths.backup.read_bytes() == original_backup
    written = config.read_text(encoding="utf-8")
    assert 'env_key = "OTHER_TOKEN"' in written


def test_disable_restores_backup_exactly(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    config.write_text(ORIGINAL_TOML, encoding="utf-8")
    enable(codex_home=codex_home, health_check=_healthy)
    assert config.read_text(encoding="utf-8") != ORIGINAL_TOML

    disable(codex_home=codex_home)

    assert config.read_text(encoding="utf-8") == ORIGINAL_TOML
    assert not resolve_paths(codex_home=codex_home).backup.exists()


def test_disable_without_backup_exits_nonzero(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    with pytest.raises(HarnessCodexError, match=r"backup|not enabled"):
        disable(codex_home=codex_home)


def test_disable_removes_config_created_when_none_existed(tmp_path: Path) -> None:
    codex_home = _codex_home(tmp_path)
    enable(codex_home=codex_home, health_check=_healthy)
    assert (codex_home / "config.toml").is_file()
    disable(codex_home=codex_home)
    assert not (codex_home / "config.toml").exists()


def test_status_never_includes_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", SECRET)
    codex_home = _codex_home(tmp_path)
    enable(codex_home=codex_home, health_check=_healthy)
    report = status(codex_home=codex_home)
    rendered = format_status(report)
    assert SECRET not in rendered
    assert SECRET not in str(report)
    assert report.token_env_set is True
    assert report.provider == "verdict"
    assert report.base_url == DEFAULT_BASE_URL
    assert "yes" in rendered.lower()
    assert DEFAULT_TOKEN_ENV in rendered


def test_status_reports_token_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    codex_home = _codex_home(tmp_path)
    enable(codex_home=codex_home, health_check=_healthy)
    report = status(codex_home=codex_home)
    rendered = format_status(report)
    assert report.token_env_set is False
    assert "no" in rendered.lower()
    assert SECRET not in rendered


def test_cli_surface_is_harness_codex_enable_disable_status(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "codex", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("enable", "disable", "status"):
        assert name in out


def test_cli_enable_help_defaults_to_verdict_not_omniroute(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "codex", "enable", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--base-url" in out
    assert "--token-env" in out
    assert "--force" in out
    assert "http://127.0.0.1:8000/v1" in out
    assert "LLMGATE_AUTH_TOKEN" in out
    assert "20128" not in out


def test_cli_enable_writes_and_status_hides_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", SECRET)
    monkeypatch.setattr("verdict.harness_codex.probe_health", lambda **_kwargs: True)
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "codex", "enable"])
    cli.main()
    enable_out = capsys.readouterr().out
    assert SECRET not in enable_out
    written = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "verdict"' in written
    assert 'base_url = "http://127.0.0.1:8000/v1"' in written
    assert "20128" not in written

    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "codex", "status"])
    cli.main()
    status_out = capsys.readouterr().out
    assert SECRET not in status_out
    assert "verdict" in status_out
    assert "http://127.0.0.1:8000/v1" in status_out
    assert "yes" in status_out.lower()


def test_cli_disable_without_enable_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(_home(tmp_path)))
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "codex", "disable"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
    err = capsys.readouterr()
    combined = err.out + err.err
    assert "backup" in combined.lower() or "not enabled" in combined.lower()
    assert SECRET not in combined
