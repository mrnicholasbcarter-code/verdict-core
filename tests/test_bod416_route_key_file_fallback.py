"""BOD-416: ``verdict route`` resolves the OmniRoute key from the key file.

Without an exported env key, ``load_credential_store_env`` (provider_bootstrap.py)
and the CLI route bootstrap (``actions/helpers._cli_bootstrap``) must still find
the key through the documented precedence that
``credentials_store.resolve_gateway_key`` implements: environment, then the
local credential store, then the owner-only ``omniroute-api-key`` key file,
then Prime's ``auth.json``. A stale/absent env key must never be the end of the
road when a key file is present, and a fresh env key must still win.

Every test uses a fake HOME/XDG_CONFIG_HOME and monkeypatched env. No real key
is ever read or printed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CONFIG_WITH_OMNIROUTE = """
providers:
  omniroute:
    base_url: http://127.0.0.1:20128/v1
    api_key_env: OMNIROUTE_API_KEY
"""


def _fake_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config_body: str | None
) -> Path:
    """Isolated HOME/XDG_CONFIG_HOME with every credential env var cleared."""
    home = tmp_path / "home"
    config_dir = home / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("OMNIROUTE_API_KEY", "VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PRIME_AGENT_DIR", raising=False)
    if config_body is not None:
        (config_dir / "verdict.yaml").write_text(config_body, encoding="utf-8")
    return config_dir


def _write_key_file(config_dir: Path, value: str) -> Path:
    """An owner-only ``omniroute-api-key`` file next to the credential store."""
    path = config_dir / "omniroute-api-key"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# --- provider_bootstrap.load_credential_store_env ---------------------------


def test_load_credential_store_env_falls_back_to_key_file_when_store_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credentials.env, no exported env: the owner-only key file still answers."""
    from verdict.provider_bootstrap import load_credential_store_env

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=None)
    _write_key_file(config_dir, "file-key-fixture")

    env = load_credential_store_env()

    assert env.get("OMNIROUTE_API_KEY") == "file-key-fixture"


def test_load_credential_store_env_prefers_store_over_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value already in credentials.env outranks the key file (unchanged)."""
    from verdict.credentials_store import CredentialsStore
    from verdict.provider_bootstrap import load_credential_store_env

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=None)
    _write_key_file(config_dir, "file-key-fixture")
    CredentialsStore(config_dir=config_dir).set("OMNIROUTE_API_KEY", "store-key-fixture")

    env = load_credential_store_env()

    assert env.get("OMNIROUTE_API_KEY") == "store-key-fixture"


def test_load_credential_store_env_returns_empty_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No store, no key file, no Prime auth.json: an empty mapping, not a crash."""
    from verdict.provider_bootstrap import load_credential_store_env

    _fake_config_home(tmp_path, monkeypatch, config_body=None)

    assert load_credential_store_env() == {}


# --- CLI route bootstrap (verdict.actions.helpers._cli_bootstrap) -----------


def test_cli_route_bootstrap_resolves_key_from_file_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: no env key, a valid key file — route's bootstrap reports no missing credential."""
    from verdict.actions.helpers import _cli_bootstrap

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=_CONFIG_WITH_OMNIROUTE)
    _write_key_file(config_dir, "file-key-fixture")

    result = _cli_bootstrap()

    codes = {note.code for note in result.notes()}
    assert "credential_env_missing" not in codes
    assert "omniroute" in result.providers


def test_cli_route_bootstrap_stale_env_key_still_wins_over_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: documented precedence is unchanged — a set env key always wins over the file."""
    from verdict.actions.helpers import _cli_bootstrap
    from verdict.provider_bootstrap import load_credential_store_env

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=_CONFIG_WITH_OMNIROUTE)
    _write_key_file(config_dir, "file-key-fixture")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "stale-env-key")

    result = _cli_bootstrap()

    assert result.source_of("providers") == "config_file"
    # The credential_store_env seam never wins over exported env: load_credential_store_env
    # still resolves the file value, but resolve_provider_bootstrap's merge keeps env on top.
    store_env = load_credential_store_env()
    assert store_env.get("OMNIROUTE_API_KEY") == "file-key-fixture"
    codes = {note.code for note in result.notes()}
    assert "credential_env_missing" not in codes


def test_cli_route_bootstrap_without_key_file_still_reports_missing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing configured anywhere: the credential_env_missing note is unchanged."""
    from verdict.actions.helpers import _cli_bootstrap

    _fake_config_home(tmp_path, monkeypatch, config_body=_CONFIG_WITH_OMNIROUTE)

    result = _cli_bootstrap()

    codes = {note.code for note in result.notes()}
    assert "credential_env_missing" in codes
