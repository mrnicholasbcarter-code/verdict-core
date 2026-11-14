"""BOD-413: test credentials.test action falls through to gateway key resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict import credentials_registry
from verdict.actions import run_action

FILE_KEY = "file-key-fixture"


@pytest.fixture
def key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "verdict").mkdir(parents=True, mode=0o700)
    path = config / "verdict" / "omniroute-api-key"
    path.write_text(FILE_KEY + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))


def test_credentials_test_resolves_key_file(
    key_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)

    called: list[str] = []

    def fake_live_check(value: str) -> tuple[bool, str]:
        called.append(value)
        return True, "ok"

    monkeypatch.setattr(
        credentials_registry,
        "CREDENTIALS",
        tuple(
            c
            if c.env_name != "OMNIROUTE_API_KEY"
            else type(c)(
                env_name=c.env_name,
                purpose=c.purpose,
                features=c.features,
                optional=c.optional,
                live_check=fake_live_check,
            )
            for c in credentials_registry.CREDENTIALS
        ),
    )

    result = run_action("credentials.test", {"name": "OMNIROUTE_API_KEY"})
    assert result.ok
    assert result.data["status"] == "ok"
    assert called == [FILE_KEY]


def test_credentials_test_env_still_wins(key_file: None, monkeypatch: pytest.MonkeyPatch) -> None:
    env_key = "env-key-sentinel"
    monkeypatch.setenv("OMNIROUTE_API_KEY", env_key)
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)

    called: list[str] = []

    def fake_live_check(value: str) -> tuple[bool, str]:
        called.append(value)
        return True, "ok"

    monkeypatch.setattr(
        credentials_registry,
        "CREDENTIALS",
        tuple(
            c
            if c.env_name != "OMNIROUTE_API_KEY"
            else type(c)(
                env_name=c.env_name,
                purpose=c.purpose,
                features=c.features,
                optional=c.optional,
                live_check=fake_live_check,
            )
            for c in credentials_registry.CREDENTIALS
        ),
    )

    result = run_action("credentials.test", {"name": "OMNIROUTE_API_KEY"})
    assert result.ok
    assert result.data["status"] == "ok"
    assert called == [env_key]


def test_credentials_test_non_gateway_name_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called: list[str] = []

    def fake_live_check(value: str) -> tuple[bool, str]:
        called.append(value)
        return True, "ok"

    monkeypatch.setattr(
        credentials_registry,
        "CREDENTIALS",
        tuple(
            c
            if c.env_name != "OPENAI_API_KEY"
            else type(c)(
                env_name=c.env_name,
                purpose=c.purpose,
                features=c.features,
                optional=c.optional,
                live_check=fake_live_check,
            )
            for c in credentials_registry.CREDENTIALS
        ),
    )
    result = run_action("credentials.test", {"name": "OPENAI_API_KEY"})
    assert not result.ok
    assert result.data["status"] == "missing"
    assert called == []
