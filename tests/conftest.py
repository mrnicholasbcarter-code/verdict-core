"""Test environment isolation - remove operator env leaks."""

import os
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolate_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove LLMGATE_AUTH_TOKEN to prevent operator shell leakage into test suite."""
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _isolate_verdict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the unverified-dev opt-in off unless a test sets it explicitly, and
    give the API an explicit in-memory receipts store (production code no longer
    detects pytest via PYTEST_CURRENT_TEST)."""
    monkeypatch.delenv("VERDICT_ALLOW_UNVERIFIED_DEV", raising=False)
    monkeypatch.setenv("VERDICT_RECEIPTS_DB", ":memory:")


_ORIGINAL_TESTCLIENT_INIT = TestClient.__init__


def _loopback_testclient_init(self: TestClient, *args: object, **kwargs: object) -> None:
    # Starlette's default peer is ("testclient", 50000), which is not an IP.
    # Production auth treats only real loopback IPs as loopback, so default the
    # synthetic peer to 127.0.0.1. Tests that pass client=... keep their value.
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    _ORIGINAL_TESTCLIENT_INIT(self, *args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _loopback_testclient_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make TestClient default to a real loopback peer address."""
    monkeypatch.setattr(TestClient, "__init__", _loopback_testclient_init)


class LiveNetworkAttempted(BaseException):
    """Raised when a guarded test tries to open a real connection.

    Deliberately a ``BaseException``: the gateway probe path catches ``Exception``
    and reports it as "did not answer", which would turn an accidental live call
    into a silently passing test. A ``BaseException`` escapes that handler, so the
    test fails loudly and names the address it tried to reach.
    """


@pytest.fixture
def no_gateway_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real outbound connection an immediate, loud failure.

    Blocks the two ways a gateway probe could reach the network: ``httpx``'s
    request entry points, and the socket layer underneath them. ASGI in-process
    clients (``TestClient``) do not open sockets, so they are unaffected.
    """
    import socket

    import httpx

    def refuse(target: object) -> None:
        raise LiveNetworkAttempted(
            f"this test must not open a connection, but it tried to reach {target!r}"
        )

    def refuse_request(*args: object, **kwargs: object) -> None:
        url = kwargs.get("url")
        if url is None and len(args) > 1:
            url = args[1]
        refuse(url)

    for name in ("get", "post", "head", "request", "stream"):
        monkeypatch.setattr(httpx, name, refuse_request, raising=False)
    monkeypatch.setattr(httpx.Client, "send", refuse_request, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_request, raising=False)
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, address: refuse(address), raising=False
    )
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: refuse(address), raising=False
    )
    monkeypatch.setattr(
        socket, "create_connection", lambda address, *a, **k: refuse(address), raising=False
    )


@pytest.fixture(autouse=True)
def _isolate_subscription_ledger():
    from verdict.cost_ledger import _subscription_reserved, subscription_budgets

    subscription_budgets.clear()
    _subscription_reserved.clear()
    yield
    subscription_budgets.clear()
    _subscription_reserved.clear()


_REAL_PRIME_MODELS = Path(os.path.expanduser("~")) / ".prime" / "agent" / "models.json"


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests never read or write the operator's real home (~/.prime, ~/.verdict).

    Code that defaults to ``Path.home()`` (for example Prime visibility refresh
    writing ``~/.prime/agent/models.json``) otherwise rewrites the operator's
    live Prime registry with fixture rows.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in ("PRIME_AGENT_HOME", "PRIME_HOME", "PRIME_AGENT_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _real_prime_registry_untouched() -> Any:
    """Fail the run if any test modified the real Prime model registry."""
    before = _fingerprint(_REAL_PRIME_MODELS)
    yield
    after = _fingerprint(_REAL_PRIME_MODELS)
    assert before == after, f"a test modified the real {_REAL_PRIME_MODELS}"
