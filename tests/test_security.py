from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from verdict.logger import log_decision
from verdict.models import RoutingDecision
from verdict.proxy import UpstreamProxy
from verdict.security import fingerprint_text, host_is_allowed, pin_upstream_url


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


def test_lifespan_rejects_anonymous_non_loopback_configuration(monkeypatch) -> None:
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_HOST", "0.0.0.0")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)

    with pytest.raises(ValueError, match="loopback-only"), TestClient(api.app):
        pass


def test_anonymous_non_loopback_client_is_rejected(monkeypatch) -> None:
    """BOD-202: anonymous mode must block requests from non-loopback peers."""
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app, client=("203.0.113.5", 5000)) as client:
        response = client.get("/v1/models")

    assert response.status_code == 403


def test_anonymous_loopback_client_is_allowed(monkeypatch) -> None:
    """BOD-202: anonymous mode must allow loopback peers."""
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app, client=("127.0.0.1", 5000)) as client:
        response = client.get("/v1/models")

    assert response.status_code not in {401, 403}


def test_health_is_open_for_non_loopback_client(monkeypatch) -> None:
    """BOD-202: /health must remain accessible regardless of peer address."""
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)

    with TestClient(api.app, client=("203.0.113.5", 5000)) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_upstream_rejects_credentials_unsafe_schemes_and_private_hosts() -> None:
    with pytest.raises(ValueError, match="scheme"):
        UpstreamProxy("file:///etc/passwd")
    with pytest.raises(ValueError, match="credentials"):
        UpstreamProxy("https://user:password@example.com/v1")
    with pytest.raises(ValueError, match="private"):
        UpstreamProxy("https://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError, match="private"):
        UpstreamProxy("https://100.64.0.1/v1")
    with pytest.raises(ValueError, match="private"):
        UpstreamProxy("https://224.0.0.1/v1")


def test_redaction_removes_secrets_from_exception_text() -> None:
    message = api.redact_text(
        "Authorization: Bearer *** https://user:password@example.com/?api_key=provider-secret"
    )

    assert "provider-secret" not in message
    assert "password@example.com" not in message


def test_redaction_removes_quoted_mapping_secrets() -> None:
    message = api.redact_text("{\"api_key\":\"sk-123\", 'token': 'abc'}")

    assert "sk-123" not in message
    assert "abc" not in message
    assert message.count("[redacted]") == 2


def test_unix_socket_auth_mode_is_not_accepted_without_real_peer_auth() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
        with pytest.raises(ValueError, match="not supported"):
            api.validate_server_security(
                host="127.0.0.1", token=None, allow_anonymous=False, unix_socket="/tmp/verdict.sock"
            )


def test_upstream_hostname_resolution_returns_the_validated_addresses(monkeypatch) -> None:
    monkeypatch.setattr(
        "verdict.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )

    assert host_is_allowed("models.example", set()) == ("93.184.216.34",)


def test_pin_upstream_url_connects_to_validated_ip_and_preserves_tls_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "verdict.security.host_is_allowed", lambda _host, _allowed: ("93.184.216.34",)
    )

    pinned, headers, extensions = pin_upstream_url("https://models.example:8443/v1/models", set())

    assert pinned == "https://93.184.216.34:8443/v1/models"
    assert headers == {"host": "models.example:8443"}
    assert extensions == {"sni_hostname": "models.example"}


def test_proxy_builds_real_request_against_pinned_destination(monkeypatch) -> None:
    proxy = UpstreamProxy("https://models.example/v1", api_key="server-secret")
    monkeypatch.setattr(
        "verdict.proxy.pin_upstream_url",
        lambda url, _allowed: (
            url.replace("models.example", "93.184.216.34"),
            {"host": "models.example"},
            {"sni_hostname": "models.example"},
        ),
        raising=False,
    )

    request = proxy._build_request("GET", "models")

    assert str(request.url) == "https://93.184.216.34/v1/models"
    assert request.headers["host"] == "models.example"
    assert request.headers["authorization"] == "Bearer server-secret"
    assert request.extensions["sni_hostname"] == "models.example"


def test_fingerprint_text_is_stable_and_non_plaintext() -> None:
    fingerprint = fingerprint_text("prompt-secret")

    assert fingerprint == fingerprint_text("prompt-secret")
    assert fingerprint != fingerprint_text("prompt-secret-2")
    assert fingerprint.startswith("sha256:")
    assert "prompt-secret" not in fingerprint


def test_invalid_omniroute_url_fails_startup_closed(monkeypatch) -> None:
    """C5: a configured but invalid OMNIROUTE_BASE_URL must fail startup, not
    silently serve routing without the eligibility gate."""
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://203.0.113.9:1/v1")
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    client = TestClient(api.app)
    with pytest.raises(RuntimeError, match="invalid OmniRoute configuration"):
        client.__enter__()
    assert api.eligibility_gate_instance is None


def test_invalid_omniroute_hostname_still_fails_startup_closed(monkeypatch) -> None:
    """C5: a non-allowlisted https/plain-http hostname must still fail startup
    closed. Only the documented ``localhost`` loopback hostname is normalised;
    an arbitrary hostname is not."""
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://evil.example:1")
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    client = TestClient(api.app)
    with pytest.raises(RuntimeError, match="invalid OmniRoute configuration"):
        client.__enter__()
    assert api.eligibility_gate_instance is None


def test_documented_omniroute_localhost_url_boots_with_gate(monkeypatch) -> None:
    """C5 fix: OMNIROUTE_BASE_URL=http://localhost:20128 is the value every
    documented guide recommends (install.sh, AGENTS.md, README.md). It must
    boot successfully WITH the eligibility gate attached, not raise."""
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:20128")
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app):
        assert api.eligibility_gate_instance is not None


def test_llmgate_upstream_fallback_without_omniroute_boots_without_gate(monkeypatch) -> None:
    """C5 fix: LLMGATE_UPSTREAM_BASE_URL pointing at a public https upstream
    (no OMNIROUTE_BASE_URL set) is a legitimate direct-upstream proxy config,
    not a misconfigured OmniRoute. It must boot without raising, with no
    availability cache or eligibility gate."""
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.setenv("LLMGATE_UPSTREAM_BASE_URL", "https://api.openai.com/v1")

    with TestClient(api.app):
        assert api.eligibility_gate_instance is None
        assert api.availability_cache_instance is None


def test_testclient_hostname_is_not_loopback(monkeypatch) -> None:
    """C6: the synthetic "testclient" peer must not be treated as loopback."""
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app, client=("testclient", 1)) as client:
        response = client.get("/v1/models")

    assert response.status_code == 403


def test_authenticated_mode_requires_receipts_db(monkeypatch) -> None:
    """C6: the in-memory receipts store is selected only by explicit config,
    never by detecting pytest."""
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "caller-secret")
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.delenv("VERDICT_RECEIPTS_DB", raising=False)
    monkeypatch.delenv("VERDICT_EVIDENCE_DB", raising=False)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with pytest.raises(RuntimeError, match="VERDICT_RECEIPTS_DB"), TestClient(api.app):
        pass
