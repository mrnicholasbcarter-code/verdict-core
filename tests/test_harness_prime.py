"""Unit tests for Prime Agent Verdict-managed harness switching.

CLI surface:
  verdict harness prime {discover,enable,disable,status,certify}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from verdict import cli
from verdict.harness_prime import (
    DEFAULT_BASE_URL,
    DEFAULT_TOKEN_ENV,
    PROVIDER_ID,
    HarnessPrimeError,
    certify,
    disable,
    discover,
    enable,
    format_certify,
    format_status,
    resolve_paths,
    status,
)

SECRET = "super-secret-prime-token-do-not-print"
ORIGINAL_MODELS = """\
{
  "providers": {
    "ollama": {
      "baseUrl": "http://127.0.0.1:11434/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "models": [{"id": "llama3.1:8b"}]
    }
  }
}
"""


def _prime_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".prime" / "agent"
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


def test_enable_upserts_provider_and_backup(tmp_path: Path) -> None:
    prime_home = _prime_home(tmp_path)
    models = prime_home / "models.json"
    models.write_text(ORIGINAL_MODELS, encoding="utf-8")

    result = enable(prime_home=prime_home, health_check=_healthy)
    assert result.created_backup is True
    assert result.backup_path.read_text(encoding="utf-8") == ORIGINAL_MODELS

    data = json.loads(models.read_text(encoding="utf-8"))
    assert "ollama" in data["providers"]
    verdict = data["providers"][PROVIDER_ID]
    assert verdict["baseUrl"] == DEFAULT_BASE_URL
    assert verdict["api"] == "openai-completions"
    assert verdict["apiKey"] == DEFAULT_TOKEN_ENV
    assert SECRET not in models.read_text(encoding="utf-8")
    assert "20128" not in verdict["baseUrl"]


def test_enable_refuses_omniroute_and_unhealthy(tmp_path: Path) -> None:
    prime_home = _prime_home(tmp_path)
    (prime_home / "models.json").write_text(ORIGINAL_MODELS, encoding="utf-8")
    with pytest.raises(HarnessPrimeError, match=r"OmniRoute|20128"):
        enable(
            prime_home=prime_home,
            base_url="http://127.0.0.1:20128/v1",
            health_check=_healthy,
            force=False,
        )
    with pytest.raises(HarnessPrimeError, match="health"):
        enable(prime_home=prime_home, health_check=_unhealthy, force=False)


def test_disable_restores_backup_exactly(tmp_path: Path) -> None:
    prime_home = _prime_home(tmp_path)
    models = prime_home / "models.json"
    models.write_text(ORIGINAL_MODELS, encoding="utf-8")
    enable(prime_home=prime_home, health_check=_healthy)
    disable(prime_home=prime_home)
    assert models.read_text(encoding="utf-8") == ORIGINAL_MODELS
    assert not resolve_paths(prime_home=prime_home).backup.exists()


def test_discover_status_certify_graceful_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, SECRET)
    prime_home = _prime_home(tmp_path)
    (prime_home / "models.json").write_text(ORIGINAL_MODELS, encoding="utf-8")
    enable(prime_home=prime_home, health_check=_healthy)

    missing = discover(prime_home=prime_home, which=lambda _name: None)
    assert missing.installed is False
    assert SECRET not in str(missing)

    found = discover(prime_home=prime_home, which=lambda _name: "/tmp/prime")
    assert found.installed is True
    assert found.pointing_at_verdict is True

    report = status(prime_home=prime_home)
    assert report.enabled is True
    assert SECRET not in format_status(report)

    not_installed = certify(prime_home=prime_home, health_check=_healthy, which=lambda _name: None)
    assert not_installed.overall == "not-installed"
    assert SECRET not in format_certify(not_installed)

    partial = certify(
        prime_home=prime_home, health_check=_healthy, which=lambda _name: "/tmp/prime"
    )
    assert partial.overall == "partial"
    assert any("secrets" in item for item in partial.needs_owner)


def test_cli_help_lists_prime_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "prime", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("discover", "enable", "disable", "status", "certify"):
        assert name in out
    assert SECRET not in out
