"""Test environment isolation - remove operator env leaks."""

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
