"""Test environment isolation - remove operator env leaks."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove LLMGATE_AUTH_TOKEN to prevent operator shell leakage into test suite."""
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
