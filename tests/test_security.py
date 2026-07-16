from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import llm_gate.api as api
from llm_gate.logger import log_decision
from llm_gate.models import RoutingDecision
from llm_gate.proxy import UpstreamProxy


def test_proxy_requires_bearer_token_by_default(monkeypatch) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app) as client:
        response = client.get("/v1/models")

    assert response.status_code == 401
    assert response.json() == {
        "error": {"message": "authentication required", "type": "authentication_error"}
    }


def test_proxy_rejects_invalid_bearer_without_leaking_token(monkeypatch) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app) as client:
        response = client.get("/v1/models", headers={"authorization": "Bearer wrong-secret"})

    assert response.status_code == 403
    assert "wrong-secret" not in response.text


def test_decision_logging_never_writes_full_prompt(tmp_path) -> None:
    decision = RoutingDecision(model="model", provider="provider", tier=2, reason="safe")
    path = tmp_path / "decisions.jsonl"
    log_decision(path, "prompt-secret", 2, decision, log_full_task=True)
    contents = path.read_text()
    assert "prompt-secret" not in contents
    assert "[redacted]" in contents


def test_anonymous_mode_is_loopback_only_and_explicit(monkeypatch) -> None:
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)

    with pytest.raises(ValueError, match="loopback"):
        api.validate_server_security(host="0.0.0.0")


def test_upstream_rejects_credentials_unsafe_schemes_and_private_hosts() -> None:
    with pytest.raises(ValueError, match="scheme"):
        UpstreamProxy("file:///etc/passwd")
    with pytest.raises(ValueError, match="credentials"):
        UpstreamProxy("https://user:password@example.com/v1")
    with pytest.raises(ValueError, match="private"):
        UpstreamProxy("https://169.254.169.254/latest/meta-data")


def test_redaction_removes_secrets_from_exception_text() -> None:
    message = api.redact_text(
        "Authorization: Bearer caller-secret https://user:password@example.com/?api_key=provider-secret"
    )
    assert "caller-secret" not in message
    assert "provider-secret" not in message
    assert "password" not in message
