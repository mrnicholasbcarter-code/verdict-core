"""The 2 moved getenv(OMNIROUTE_API_KEY) sites resolve via the one resolver (BOD-411 AC3).

``omniroute_endpoint_from_env`` (free_tier_admit.py) and the unauthorized-key
diagnostic path (intelligence.py) must prefer a key-file credential over a
stale/absent ``OMNIROUTE_API_KEY`` env var, exactly as
``credentials_store.resolve_gateway_key`` documents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict.free_tier_admit import omniroute_endpoint_from_env

FILE_KEY = "file-key-fixture"


@pytest.fixture
def key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "verdict").mkdir(parents=True, mode=0o700)
    path = config / "verdict" / "omniroute-api-key"
    path.write_text(FILE_KEY + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))


def test_omniroute_endpoint_from_env_uses_key_file_when_env_key_absent(
    key_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env key at all: the resolver falls through to the owner-only key file."""
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    assert omniroute_endpoint_from_env(None) == ("http://127.0.0.1:20128", FILE_KEY)


def test_omniroute_endpoint_from_env_prefers_verdict_env_key_over_key_file(
    key_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh VERDICT_OMNIROUTE_API_KEY still wins over the key file (documented precedence)."""
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "verdict-env-key")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    assert omniroute_endpoint_from_env(None) == ("http://127.0.0.1:20128", "verdict-env-key")


def test_intelligence_unauthorized_key_source_reports_key_file_when_env_absent(
    key_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unauthorized-key diagnostic names the key-file source, not a stale env guess."""
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)

    from verdict.credentials_store import resolve_gateway_key

    resolved = resolve_gateway_key()
    assert resolved is not None
    value, kind = resolved
    assert value == FILE_KEY
    assert kind == "key_file"


def test_intelligence_unauthorized_key_source_reports_env_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set OMNIROUTE_API_KEY is reported as the env source, matching intelligence.py's label."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "fresh-env-key")

    from verdict.credentials_store import resolve_gateway_key

    resolved = resolve_gateway_key()
    assert resolved is not None
    value, kind = resolved
    assert value == "fresh-env-key"
    assert kind == "env:OMNIROUTE_API_KEY"
