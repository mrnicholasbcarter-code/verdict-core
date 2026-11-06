"""BOD-416: ``verdict route`` resolves the OmniRoute key from the key file.

Without an exported env key, ``load_credential_store_env`` (provider_bootstrap.py)
and the CLI route bootstrap (``actions/helpers._cli_bootstrap``) must still find
the key through the documented precedence that
``credentials_store.resolve_gateway_key`` implements: environment, then the
local credential store, then the owner-only ``omniroute-api-key`` key file,
then Prime's ``auth.json``. A stale/absent env key must never be the end of the
road when a key file is present, and a fresh env key must still win.

The route's catalog discovery (``verdict.discovery.fetch_models``) is held to
the same precedence: a provider that declares a gateway key env name gets the
key-file key when that env name is unset, so a key-file-only route sees the
same catalog (and makes the same decision) as one with the key exported.

Every test uses a fake HOME/XDG_CONFIG_HOME and monkeypatched env. No real key
is ever read or printed, and gateway requests are stubbed (no sockets).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

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


# --- route catalog discovery (verdict.discovery.fetch_models) ----------------

_CATALOG_MODEL = "groq/llama-3.3-70b-versatile"


class _ModelsResponse:
    """Minimal ``open_no_redirect`` response carrying a one-model catalog."""

    def __enter__(self) -> _ModelsResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"object": "list", "data": [{"id": _CATALOG_MODEL}]}).encode()


def _stub_gateway(monkeypatch: pytest.MonkeyPatch, *, accepts: str) -> list[str | None]:
    """Stub discovery's transport: 200 only for ``Bearer <accepts>``, else 401.

    Returns the Authorization header each discovery request carried.
    """
    import verdict.discovery

    seen: list[str | None] = []

    def open_fake(request: urllib.request.Request, timeout: float | None = None) -> Any:
        header = request.get_header("Authorization")
        seen.append(header)
        if header != f"Bearer {accepts}":
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", Message(), None)
        return _ModelsResponse()

    monkeypatch.setattr(verdict.discovery, "_CACHE", {})
    monkeypatch.setattr(verdict.discovery, "open_no_redirect", open_fake)
    return seen


def test_route_catalog_discovery_sends_key_file_key_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI route gate's omniroute discovery authenticates with the key file."""
    from verdict.actions.helpers import build_route_gate
    from verdict.discovery import fetch_models

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=_CONFIG_WITH_OMNIROUTE)
    _write_key_file(config_dir, "file-key-fixture")
    seen = _stub_gateway(monkeypatch, accepts="file-key-fixture")

    gate = build_route_gate()
    models = fetch_models("omniroute", gate.providers["omniroute"])

    assert seen == ["Bearer file-key-fixture"]
    assert [m.id for m in models] == [_CATALOG_MODEL]


def test_route_catalog_discovery_stale_env_key_still_wins_over_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 for discovery: an exported key is sent even when a key file exists."""
    from verdict.actions.helpers import build_route_gate
    from verdict.discovery import fetch_models

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=_CONFIG_WITH_OMNIROUTE)
    _write_key_file(config_dir, "file-key-fixture")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "stale-env-key")
    seen = _stub_gateway(monkeypatch, accepts="file-key-fixture")

    gate = build_route_gate()
    models = fetch_models("omniroute", gate.providers["omniroute"])

    assert seen == ["Bearer stale-env-key"]
    assert models == []


def test_catalog_discovery_never_sends_the_gateway_key_to_other_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a provider that declares a gateway key env name gets the key-file key."""
    from verdict.discovery import fetch_models
    from verdict.models import ProviderConfig

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=None)
    _write_key_file(config_dir, "file-key-fixture")
    monkeypatch.delenv("OTHER_PROVIDER_API_KEY", raising=False)
    seen = _stub_gateway(monkeypatch, accepts="file-key-fixture")

    fetch_models("public_ollama", ProviderConfig(base_url="http://127.0.0.1:11434/v1"))
    fetch_models(
        "other",
        ProviderConfig(base_url="http://127.0.0.1:9/v1", api_key_env="OTHER_PROVIDER_API_KEY"),
    )

    assert seen == [None, None]


@pytest.mark.parametrize("key_source", ["exported_env", "key_file_only"])
def test_cli_route_decision_is_the_same_for_key_file_and_exported_key(
    key_source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_gateway_network: None
) -> None:
    """With no admit snapshot, the route picks from the authenticated catalog either way.

    Before the fix the key-file-only route got a 401 from discovery, saw an empty
    catalog and fell back to the primary model, while the exported key saw the
    catalog and routed to it.
    """
    from verdict.actions.helpers import build_route_gate
    from verdict.free_tier_admit import LiveAdmitError

    config_dir = _fake_config_home(tmp_path, monkeypatch, config_body=_CONFIG_WITH_OMNIROUTE)
    _write_key_file(config_dir, "file-key-fixture")
    if key_source == "exported_env":
        monkeypatch.setenv("OMNIROUTE_API_KEY", "file-key-fixture")
    monkeypatch.chdir(tmp_path)
    seen = _stub_gateway(monkeypatch, accepts="file-key-fixture")

    def no_snapshot(*args: object, **kwargs: object) -> Any:
        raise LiveAdmitError("unreachable", "fixture gateway has no admit surfaces")

    monkeypatch.setattr("verdict.intelligence.load_omniroute_admit_snapshot", no_snapshot)

    decision = build_route_gate().route("hello", "medium")

    assert seen == ["Bearer file-key-fixture"]
    assert decision.model == _CATALOG_MODEL
