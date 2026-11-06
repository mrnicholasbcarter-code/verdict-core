"""Subagent selection and the worker runtime resolve the management token like serve.

Environment first (blank ignored), then the same-named credential-store field. Both
paths run outside the CLI entry (``scripts/prime_supervisor.py`` and
``python -m verdict.worker_runtime``), so ``load_into_env`` never copies the stored
token into their environment. No test here builds a real transport or opens a socket.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import verdict.omniroute
from verdict import worker_runtime
from verdict.credentials_store import CredentialsStore
from verdict.subagent_models import SubagentModelSelector

ENV_TOKEN = "env-management-sentinel"
STORE_TOKEN = "store-management-sentinel"
NAME = "OMNIROUTE_MANAGEMENT_TOKEN"

TransportKwargs = Callable[[], dict[str, Any]]


class _RecordedError(Exception):
    pass


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CredentialsStore:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv("OMNIROUTE_USAGE_API_KEY_ID", raising=False)
    return CredentialsStore()


@pytest.fixture(params=["subagent_models", "worker_runtime"])
def transport_kwargs(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> TransportKwargs:
    """The keyword arguments each runtime path hands to its OmniRoute transport."""
    seen: list[dict[str, Any]] = []

    def record(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise _RecordedError

    if request.param == "subagent_models":
        # from_environment imports the transport from verdict.omniroute at call time.
        monkeypatch.setattr(verdict.omniroute, "OmniRouteHTTPTransport", record)

        def run() -> None:
            with pytest.raises(_RecordedError):
                SubagentModelSelector.from_environment()

    else:
        monkeypatch.setattr(worker_runtime, "OmniRouteHTTPTransport", record)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "config.json").write_text(json.dumps({"prompt": "p", "task": {}}))

        def run() -> None:
            # cli_run fails closed when the transport cannot be built.
            assert asyncio.run(worker_runtime.cli_run(run_dir)) == 1

    def build() -> dict[str, Any]:
        run()
        [kwargs] = seen
        return kwargs

    return build


def test_store_only_token_is_used(
    store: CredentialsStore, transport_kwargs: TransportKwargs
) -> None:
    store.set(NAME, STORE_TOKEN)
    kwargs = transport_kwargs()
    assert kwargs["management_token"] == STORE_TOKEN
    assert "connections" in kwargs["runtime_sources"]


def test_exported_token_wins_over_the_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, transport_kwargs: TransportKwargs
) -> None:
    store.set(NAME, STORE_TOKEN)
    monkeypatch.setenv(NAME, ENV_TOKEN)
    kwargs = transport_kwargs()
    assert kwargs["management_token"] == ENV_TOKEN
    assert "connections" in kwargs["runtime_sources"]


def test_blank_exported_token_falls_back_to_the_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, transport_kwargs: TransportKwargs
) -> None:
    store.set(NAME, STORE_TOKEN)
    monkeypatch.setenv(NAME, "   ")
    kwargs = transport_kwargs()
    assert kwargs["management_token"] == STORE_TOKEN
    assert "connections" in kwargs["runtime_sources"]


def test_blank_exported_token_without_a_store_reads_health_only(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, transport_kwargs: TransportKwargs
) -> None:
    monkeypatch.setenv(NAME, "   ")
    kwargs = transport_kwargs()
    assert kwargs["management_token"] is None
    assert set(kwargs["runtime_sources"]) == {"health"}


def test_no_token_keeps_health_only(
    store: CredentialsStore, transport_kwargs: TransportKwargs
) -> None:
    kwargs = transport_kwargs()
    assert kwargs["management_token"] is None
    assert set(kwargs["runtime_sources"]) == {"health"}


def test_group_readable_store_is_ignored_not_fatal(
    store: CredentialsStore, transport_kwargs: TransportKwargs
) -> None:
    """The resolver refuses an insecure store; the path reads health only, no crash."""
    store.set(NAME, STORE_TOKEN)
    store.store_path.chmod(0o640)
    kwargs = transport_kwargs()
    assert kwargs["management_token"] is None
    assert set(kwargs["runtime_sources"]) == {"health"}
