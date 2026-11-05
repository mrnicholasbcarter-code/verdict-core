"""verdict doctor: the gateway probe carries the OmniRoute key even when the
provider base URL is the gateway origin plus ``/v1`` (the shipped config shape)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import verdict.doctor_diagnostics as dd
import verdict.gateway_lifecycle as gl
import verdict.provider_bootstrap as pb

GATEWAY = "http://127.0.0.1:20128"


def _bootstrap(base_url: str, gateway_url: str | None = GATEWAY) -> Any:
    binding = SimpleNamespace(base_url=base_url, api_key_env="OMNIROUTE_API_KEY")
    return SimpleNamespace(providers={"omniroute": binding}, gateway_url=gateway_url)


def _captured_key(monkeypatch: pytest.MonkeyPatch, bootstrap: Any) -> str | None:
    keys: list[str | None] = []

    def fake_authenticated(api_key: str | None, **_: object) -> object:
        keys.append(api_key)
        return lambda url: None

    monkeypatch.setattr(gl, "authenticated_gateway_probe", fake_authenticated)
    dd._gateway_probe_for(bootstrap)
    assert len(keys) == 1
    return keys[0]


@pytest.fixture
def no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "load_credential_store_env", lambda: {})


def test_v1_provider_base_matches_bare_gateway_origin(
    monkeypatch: pytest.MonkeyPatch, no_store: None
) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "key-from-env")
    assert _captured_key(monkeypatch, _bootstrap(f"{GATEWAY}/v1")) == "key-from-env"


def test_bare_provider_base_still_matches(monkeypatch: pytest.MonkeyPatch, no_store: None) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "key-from-env")
    assert _captured_key(monkeypatch, _bootstrap(GATEWAY)) == "key-from-env"


def test_trailing_slashes_are_ignored(monkeypatch: pytest.MonkeyPatch, no_store: None) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "key-from-env")
    assert _captured_key(monkeypatch, _bootstrap(f"{GATEWAY}/v1/", f"{GATEWAY}/")) == "key-from-env"


def test_key_is_read_from_credential_store_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setattr(
        pb, "load_credential_store_env", lambda: {"OMNIROUTE_API_KEY": "from-store"}
    )
    assert _captured_key(monkeypatch, _bootstrap(f"{GATEWAY}/v1")) == "from-store"


def test_different_host_provider_does_not_leak_gateway_key(
    monkeypatch: pytest.MonkeyPatch, no_store: None
) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "key-from-env")
    assert _captured_key(monkeypatch, _bootstrap("http://10.0.0.5:20128/v1")) is None


def test_no_gateway_url_sends_no_key(monkeypatch: pytest.MonkeyPatch, no_store: None) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "key-from-env")
    assert _captured_key(monkeypatch, _bootstrap(f"{GATEWAY}/v1", None)) is None
