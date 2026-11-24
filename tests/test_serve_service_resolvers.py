"""The one place workers learn serve's address and credential (review items 1 and 11).

Everything is isolated: HOME, VERDICT_HOME and the environment point at temp state, so no real
credential store, ``serve.env`` or exported token is ever read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict import serve_service

TOKEN = "serve-token-from-test-0123456789"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / "verdict"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in ("LLMGATE_AUTH_TOKEN", "VERDICT_SERVE_URL", "OMNIROUTE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _write_serve_env(home: Path, token: str) -> None:
    path = serve_service.env_file_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(f"{serve_service.TOKEN_ENV}={token}\n")


def test_no_token_anywhere_is_none(tmp_path: Path) -> None:
    assert serve_service.resolve_serve_token(home=tmp_path / "home") is None


def test_the_environment_wins_over_the_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_serve_env(home, "from-the-env-file")
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", f"  {TOKEN}  ")
    assert serve_service.resolve_serve_token(home=home) == (TOKEN, "environment")


def test_serve_env_is_the_last_resort(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_serve_env(home, TOKEN)
    assert serve_service.resolve_serve_token(home=home) == (TOKEN, "serve_env")


def test_the_credential_store_is_read_before_serve_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_serve_env(home, "from-the-env-file")
    monkeypatch.setattr(
        "verdict.credentials_store.CredentialsStore.load",
        lambda self: {serve_service.TOKEN_ENV: TOKEN},
    )
    assert serve_service.resolve_serve_token(home=home) == (TOKEN, "credential_store")


def test_the_omniroute_gateway_key_is_not_a_serve_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "gateway-key")
    assert serve_service.resolve_serve_token() is None


def test_an_unreadable_credential_store_does_not_hide_the_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_serve_env(home, TOKEN)

    def unreadable(self: object) -> dict[str, str]:
        raise PermissionError("store is not readable")

    monkeypatch.setattr("verdict.credentials_store.CredentialsStore.load", unreadable)
    assert serve_service.resolve_serve_token(home=home) == (TOKEN, "serve_env")


def test_serve_origin_defaults_to_the_unit_address() -> None:
    assert serve_service.resolve_serve_origin() == "http://127.0.0.1:8000"
    assert serve_service.resolve_serve_origin() == serve_service.DEFAULT_ORIGIN


def test_serve_origin_prefers_the_flag_then_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_SERVE_URL", "http://127.0.0.1:9100/v1")
    assert serve_service.resolve_serve_origin() == "http://127.0.0.1:9100"
    assert (
        serve_service.resolve_serve_origin("HTTP://Localhost:9200/v1/") == "http://localhost:9200"
    )


@pytest.mark.parametrize("bad", ["ftp://127.0.0.1:8000", "127.0.0.1:8000", "http://", "not a url"])
def test_a_bad_serve_address_is_refused_not_replaced_by_the_default(bad: str) -> None:
    with pytest.raises(serve_service.ServeServiceError):
        serve_service.resolve_serve_origin(bad)


def test_a_valid_environment_token_is_not_hidden_by_an_undecodable_credential_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each source is read only when the ones above it gave nothing: a malformed lower file
    must neither raise nor stop a valid environment token being used."""
    calls: list[str] = []

    def undecodable(self: object) -> dict[str, str]:
        calls.append("store")
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "malformed store")

    monkeypatch.setattr("verdict.credentials_store.CredentialsStore.load", undecodable)
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", TOKEN)
    assert serve_service.resolve_serve_token(home=tmp_path / "home") == (TOKEN, "environment")
    assert calls == [], "a lower-priority source was read although the environment had the token"


def test_an_undecodable_serve_env_is_skipped_not_raised(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = serve_service.env_file_path(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"LLMGATE_AUTH_TOKEN=\xff\xfe\xfd\n")
    assert serve_service.resolve_serve_token(home=home) is None


def test_an_undecodable_credential_store_falls_through_to_serve_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_serve_env(home, TOKEN)

    def undecodable(self: object) -> dict[str, str]:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "malformed store")

    monkeypatch.setattr("verdict.credentials_store.CredentialsStore.load", undecodable)
    assert serve_service.resolve_serve_token(home=home) == (TOKEN, "serve_env")
