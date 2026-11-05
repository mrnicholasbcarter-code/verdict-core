"""BOD-219: `verdict serve` resolves the OmniRoute management token like BOD-345 does.

Environment first (blank ignored), then the same-named credential-store field, so an
unattended systemd unit with no exported token still reads the connections source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict import api
from verdict.credentials_store import CredentialsStore

ENV_TOKEN = "env-management-sentinel"
STORE_TOKEN = "store-management-sentinel"


class _RecordedError(Exception):
    pass


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CredentialsStore:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN", raising=False)
    return CredentialsStore()


def _serve_transport_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []

    def record(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise _RecordedError

    monkeypatch.setattr(api, "OmniRouteHTTPTransport", record)
    with pytest.raises(RuntimeError):
        api._build_availability_cache()
    [kwargs] = seen
    return kwargs


def _diagnostic_codes() -> set[str]:
    return {d["code"] for d in api.server_bootstrap_diagnostics()["diagnostics"]}


def test_serve_prefers_the_environment_token_over_the_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", STORE_TOKEN)
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", ENV_TOKEN)
    kwargs = _serve_transport_kwargs(monkeypatch)
    assert kwargs["management_token"] == ENV_TOKEN
    assert kwargs["runtime_sources"] == {"health", "connections"}


def test_serve_uses_the_store_token_when_the_environment_has_none(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", STORE_TOKEN)
    kwargs = _serve_transport_kwargs(monkeypatch)
    assert kwargs["management_token"] == STORE_TOKEN
    assert kwargs["runtime_sources"] == {"health", "connections"}


def test_serve_ignores_a_blank_environment_token(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", STORE_TOKEN)
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "   ")
    kwargs = _serve_transport_kwargs(monkeypatch)
    assert kwargs["management_token"] == STORE_TOKEN
    assert kwargs["runtime_sources"] == {"health", "connections"}


def test_serve_reads_health_only_without_any_token(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "   ")
    kwargs = _serve_transport_kwargs(monkeypatch)
    assert kwargs["management_token"] is None
    assert kwargs["runtime_sources"] == {"health"}


def test_startup_diagnostic_reflects_a_store_token(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "runtime_sources_health_only" in _diagnostic_codes()
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", STORE_TOKEN)
    report = api.server_bootstrap_diagnostics()
    assert "runtime_sources_health_only" not in {d["code"] for d in report["diagnostics"]}
    assert STORE_TOKEN not in json.dumps(report, default=str)


def test_startup_diagnostic_treats_a_blank_environment_token_as_absent(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "  ")
    assert "runtime_sources_health_only" in _diagnostic_codes()
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", ENV_TOKEN)
    report = api.server_bootstrap_diagnostics()
    assert "runtime_sources_health_only" not in {d["code"] for d in report["diagnostics"]}
    assert ENV_TOKEN not in json.dumps(report, default=str)
