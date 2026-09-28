"""API-level relay tests for context-length body threading (BOD-203)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from tests.test_proxy import FixedIntelligence, _configure_test_app
from verdict import api
from verdict.proxy import UpstreamProxy
from verdict.relay import failure_class, retryable_response_status

# ---------------------------------------------------------------------------
# Context-length error body used by providers
# ---------------------------------------------------------------------------

CONTEXT_LENGTH_BODY = (
    '{"error":{"message":"[kiro/claude-sonnet-5-thinking] [400]: '
    'Input is too long. (reset after 73h 30m 45s)","type":"invalid_request_error"}}'
)

NORMAL_400_BODY = '{"error":{"message":"invalid parameter","type":"invalid_request_error"}}'

RATE_LIMIT_BODY = '{"error":{"message":"rate limit exceeded","type":"rate_limit_error"}}'


# ---------------------------------------------------------------------------
# Transport that returns configurable status + body per attempt
# ---------------------------------------------------------------------------


class ContextLengthTransport(httpx.AsyncBaseTransport):
    """Return a sequence of (status, body) pairs, then 200 for the rest."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        body = json.loads(raw) if raw else None
        self.requests.append({"url": str(request.url), "body": body})
        if self.responses:
            status, resp_body = self.responses.pop(0)
            if status >= 400:
                return httpx.Response(
                    status, headers={"content-type": "application/json"}, content=resp_body.encode()
                )
        # Success response
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-ok",
                "model": body.get("model", "selected-model") if body else "selected-model",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 4},
            },
        )


# ---------------------------------------------------------------------------
# Intelligence that provides alternatives for multi-attempt relay
# ---------------------------------------------------------------------------


class TwoModelIntelligence(FixedIntelligence):
    """Route to selected-model with backup-model as an alternative."""

    async def route(
        self,
        task: str,
        criticality: str = "medium",
        context: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> Any:
        from verdict.models import RoutingDecision

        return RoutingDecision(
            model="selected-model",
            provider="omniroute",
            tier=2,
            reason="test selection",
            request_id="request-ctx",
            managed_backend_status="healthy",
            quality_outcome="unknown",
            alternatives=["backup-model"],
            candidate_states=[{"model_id": "backup-model", "admitted": True, "state": "ready"}],
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContextLengthBodyClassification:
    """Context-length 400 with body is classified distinctly from normal 400."""

    def test_context_length_400_classified_as_context_length_overflow(self) -> None:
        assert failure_class(400, body=CONTEXT_LENGTH_BODY) == "context_length_overflow"

    def test_normal_400_classified_as_capability_failure(self) -> None:
        assert failure_class(400, body=NORMAL_400_BODY) == "capability_or_request_failure"

    def test_normal_400_no_body_classified_as_capability_failure(self) -> None:
        assert failure_class(400) == "capability_or_request_failure"

    def test_context_length_400_is_retryable(self) -> None:
        assert (
            retryable_response_status(400, compatibility_applied=False, body=CONTEXT_LENGTH_BODY)
            is True
        )

    def test_normal_400_not_retryable(self) -> None:
        assert (
            retryable_response_status(400, compatibility_applied=False, body=NORMAL_400_BODY)
            is False
        )

    def test_429_still_retryable(self) -> None:
        assert (
            retryable_response_status(429, compatibility_applied=False, body=RATE_LIMIT_BODY)
            is True
        )


class TestRelayContextLengthIntegration:
    """End-to-end relay loop: context-length body threaded from api.py."""

    def test_context_length_skips_same_model_tries_different(self, monkeypatch: Any) -> None:
        """Context-length 400 from model A -> skip retry on A, try model B."""
        transport = ContextLengthTransport([(400, CONTEXT_LENGTH_BODY)])
        monkeypatch.setattr(api, "_build_intelligence", lambda: TwoModelIntelligence())
        monkeypatch.setattr(
            api,
            "_build_proxy",
            lambda: UpstreamProxy("http://upstream.test/v1", api_key="k", transport=transport),
        )
        monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
        monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LLMGATE_LOG_PATH", "")

        with TestClient(api.app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "selected-model",
                    "messages": [{"role": "user", "content": "preserve all fields"}],
                },
                headers={"idempotency-key": "ctx-len-test"},
            )

        # The second attempt (backup-model) should succeed
        assert response.status_code == 200
        # Two requests: first to selected-model (400), second to backup-model (200)
        assert len(transport.requests) == 2
        models = [r["body"]["model"] for r in transport.requests]
        assert models == ["selected-model", "backup-model"]

    def test_context_length_single_model_not_retried(self, monkeypatch: Any) -> None:
        """Context-length 400 with no alternative -> no retry, return 400."""
        transport = ContextLengthTransport([(400, CONTEXT_LENGTH_BODY)])
        _configure_test_app(monkeypatch, transport)

        with TestClient(api.app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "selected-model",
                    "messages": [{"role": "user", "content": "preserve all fields"}],
                },
            )

        # No alternative available, context-length is retryable but only to different model
        # Since build_attempts deduplicates, and retryable_response_status returns True,
        # the loop tries next attempt which doesn't exist -> returns the 400 error
        assert len(transport.requests) == 1
        # Should surface the upstream error
        assert response.status_code >= 400

    def test_normal_400_not_retried(self, monkeypatch: Any) -> None:
        """Normal 400 (not context-length) is not retried even with alternatives."""
        transport = ContextLengthTransport([(400, NORMAL_400_BODY)])
        monkeypatch.setattr(api, "_build_intelligence", lambda: TwoModelIntelligence())
        monkeypatch.setattr(
            api,
            "_build_proxy",
            lambda: UpstreamProxy("http://upstream.test/v1", api_key="k", transport=transport),
        )
        monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
        monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LLMGATE_LOG_PATH", "")

        with TestClient(api.app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "selected-model",
                    "messages": [{"role": "user", "content": "preserve all fields"}],
                },
            )

        # Normal 400 is NOT retryable -> only one request
        assert response.status_code == 400
        assert len(transport.requests) == 1

    def test_429_still_triggers_cooldown_retry(self, monkeypatch: Any) -> None:
        """429 rate-limit is still retried with cooldown (unchanged behaviour)."""
        transport = ContextLengthTransport([(429, RATE_LIMIT_BODY)])
        monkeypatch.setattr(api, "_build_intelligence", lambda: TwoModelIntelligence())
        monkeypatch.setattr(
            api,
            "_build_proxy",
            lambda: UpstreamProxy("http://upstream.test/v1", api_key="k", transport=transport),
        )
        monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
        monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LLMGATE_LOG_PATH", "")

        with TestClient(api.app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "selected-model",
                    "messages": [{"role": "user", "content": "preserve all fields"}],
                },
                headers={"idempotency-key": "rate-limit-test"},
            )

        # 429 is retryable -> should try backup-model and succeed
        assert response.status_code == 200
        assert len(transport.requests) == 2
        models = [r["body"]["model"] for r in transport.requests]
        assert models == ["selected-model", "backup-model"]
