"""Contract tests for the transparent OpenAI-compatible proxy slice."""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import verdict.api as api
from verdict.intelligence import ReadinessReport
from verdict.models import RoutingDecision
from verdict.proxy import UpstreamProxy


class ChunkedSSE(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class CloseCountingStream:
    def __init__(
        self,
        chunks: list[bytes],
        error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._error = error
        self._cleanup_error = cleanup_error
        self.close_count = 0

    def __aiter__(self) -> CloseCountingStream:
        return self

    async def __anext__(self) -> bytes:
        if self._error is not None and not self._chunks:
            error, self._error = self._error, None
            raise error
        try:
            chunk = self._chunks.pop(0)
        except IndexError as exc:
            raise StopAsyncIteration from exc
        if self._error is not None and chunk == b"second":
            error, self._error = self._error, None
            raise error
        return chunk

    async def aclose(self) -> None:
        self.close_count += 1
        if self._cleanup_error is not None:
            raise self._cleanup_error


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, stream: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw_body = await request.aread()
        body = json.loads(raw_body) if raw_body else None
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-length": "999",
                    "x-upstream": "catalog",
                },
                json={
                    "object": "list",
                    "data": [
                        {"id": "selected-model"},
                        {"id": "denied-model"},
                        {"name": "invalid-row"},
                    ],
                },
            )
        response_body = b'{"id":"chatcmpl-test","choices":[],"usage":{"total_tokens":4}}'
        if self.stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "x-upstream": "stream"},
                stream=ChunkedSSE(
                    [
                        b'data: {"choices":[',
                        b'{"delta":{"tool_calls":[{"index":0}}]}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                ),
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-upstream": "complete"},
            content=response_body,
        )


class FixedIntelligence:
    primary_model = "selected-model"
    providers: ClassVar[dict[str, object]] = {}
    log_path = ""
    log_full_task = False
    discovery_ttl = 60
    profile = "test"

    async def route(
        self, task: str, criticality: str = "medium", context: dict[str, Any] | None = None
    ) -> RoutingDecision:
        assert task == "preserve all fields"
        return RoutingDecision(
            model="selected-model",
            provider="omniroute",
            tier=2,
            reason="test selection",
            request_id="request-1",
            managed_backend_status="healthy",
            quality_outcome="unknown",
        )

    def readiness(self) -> ReadinessReport:
        return ReadinessReport(
            status="ready",
            production_ready=True,
            profile="production",
            managed_backend_status="healthy",
            degraded_mode=False,
            policy_version="policy-2026-07-13.1",
            reason="ready",
            adapter_versions={"ruflo": "ruflo", "ruvector": "ruvector"},
        )


def _configure_test_app(monkeypatch, transport: RecordingTransport) -> None:
    monkeypatch.setattr(api, "_build_intelligence", lambda: FixedIntelligence())
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy(
            "http://upstream.test/v1", api_key="server-secret", transport=transport
        ),
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")


def test_proxy_preserves_unknown_request_fields_and_uses_server_auth(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    payload = {
        "model": "client-requested-model",
        "messages": [{"role": "user", "content": "preserve all fields"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "response_format": {"type": "json_object"},
        "stream": False,
        "seed": 17,
        "x-forward-compatible": {"future": True},
    }

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"authorization": "Bearer client-secret"},
        )

    assert response.status_code == 200
    assert response.headers["x-upstream"] == "complete"
    assert response.headers["x-verdict-model"] == "selected-model"
    forwarded = transport.requests[0]
    assert forwarded["headers"]["authorization"] == "Bearer server-secret"
    assert forwarded["body"] == {**payload, "model": "selected-model"}
    assert json.loads(response.content)["usage"]["total_tokens"] == 4


def test_buffered_response_exposes_correlated_redacted_evidence(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "preserve all fields api_key=sk-never-returned",
            }
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
        "correlation_id": "workflow-proxy-1",
    }

    with TestClient(api.app) as client:
        monkeypatch.setattr(api, "_task_text", lambda payload: "preserve all fields")
        response = client.post("/v1/chat/completions", json=payload)
        explain = client.get(
            "/v1/route/explain?request_id=request-1",
        )

    assert response.status_code == 200
    assert response.headers["x-verdict-correlation-id"] == "workflow-proxy-1"
    assert response.headers["x-verdict-evidence-request-id"] == "request-1"
    evidence = explain.json()
    assert evidence["routing_decision"]["request_id"] == "request-1"
    assert evidence["routing_decision"]["correlation_id"] == "workflow-proxy-1"
    assert evidence["outcome_event"]["outcome"] == "success"
    rendered = json.dumps(evidence)
    assert "sk-never-returned" not in rendered
    assert "private api_key" not in rendered


def test_route_response_keeps_legacy_body_and_tags_availability_explain(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)

    class ExplainCache:
        policy_version = "policy-test"

        def keys(self):
            return ["selected-model"]

        def explain(self, model_id):
            return {"model_id": model_id, "cache_state": "fresh"}

    monkeypatch.setattr(api, "_build_availability_cache", lambda: (ExplainCache(), None))
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/route", json={"task": "preserve all fields", "request_id": "route-request"}
        )
        availability = client.get("/v1/route/explain?model_id=selected-model")

    assert response.status_code == 200
    assert "evidence" not in response.json()
    assert response.headers["x-verdict-evidence-id"]
    assert availability.status_code == 200
    assert availability.json()["kind"] == "availability_explain"


def test_evidence_explain_is_tagged_and_has_ordered_events(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "preserve all fields"}]},
        )
        evidence = client.get(
            "/v1/route/explain?evidence_id=" + response.headers["x-verdict-evidence-id"]
        )

    assert evidence.status_code == 200
    payload = evidence.json()
    assert payload["kind"] == "execution_evidence"
    assert payload["envelope_version"] == "1"
    assert [event["event_type"] for event in payload["events"]] == [
        "execution_started",
        "chat_completion_buffered",
    ]


def test_verdict_local_identifiers_are_not_forwarded(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "request_id": "local-request",
                "correlation_id": "local-correlation",
                "criticality": "high",
            },
        )
    assert response.status_code == 200
    assert "request_id" not in transport.requests[0]["body"]
    assert "correlation_id" not in transport.requests[0]["body"]
    assert "criticality" not in transport.requests[0]["body"]


def test_duplicate_request_ids_remain_independently_finalized(monkeypatch) -> None:
    first_transport = RecordingTransport()
    _configure_test_app(monkeypatch, first_transport)
    with TestClient(api.app) as client:
        first = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "request_id": "same",
            },
        )
        second = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "request_id": "same",
                "correlation_id": "second",
            },
        )
        first_evidence = client.get(
            "/v1/route/explain?evidence_id=" + first.headers["x-verdict-evidence-id"]
        ).json()
        second_evidence = client.get(
            "/v1/route/explain?evidence_id=" + second.headers["x-verdict-evidence-id"]
        ).json()
    assert first.status_code == second.status_code == 200
    assert first_evidence["outcome_event"]["outcome"] == "success"
    assert second_evidence["routing_decision"]["correlation_id"] == "second"
    assert first.headers["x-verdict-evidence-id"] != second.headers["x-verdict-evidence-id"]
    assert first_evidence["evidence_id"] != second_evidence["evidence_id"]


def test_duplicate_request_id_selector_is_rejected_as_ambiguous(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    with TestClient(api.app) as client:
        client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "preserve all fields"}]},
        )
        client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "request_id": "request-1",
            },
        )
        response = client.get("/v1/route/explain?request_id=request-1")
    assert response.status_code == 409
    assert "evidence_id" in response.json()["error"]["message"]


def test_evidence_id_disambiguates_reused_request_id(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    with TestClient(api.app) as client:
        first = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "request_id": "same",
                "correlation_id": "first",
            },
        )
        second = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "request_id": "same",
                "correlation_id": "second",
            },
        )
        first_evidence = client.get("/v1/route/explain?correlation_id=first").json()
        selected = client.get(
            "/v1/route/explain?evidence_id=" + first_evidence["evidence_id"]
        ).json()
    assert first.status_code == second.status_code == 200
    assert selected["routing_decision"]["correlation_id"] == "first"


def test_evidence_lookup_does_not_trust_anonymous_scope_header(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "correlation_id": "anonymous-scope",
            },
        )
        evidence_id = response.headers["x-verdict-evidence-id"]
        lookup = client.get(
            "/v1/route/explain?evidence_id=" + evidence_id,
            headers={"x-verdict-scope": "forged-tenant"},
        )
    assert lookup.status_code == 200


def test_explain_rejects_ambiguous_selector(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.get("/v1/route/explain?model_id=selected-model&request_id=missing")
    assert response.status_code == 400


def test_streaming_evidence_is_finalized_after_consumption(monkeypatch) -> None:
    transport = RecordingTransport(stream=True)
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "stream": True,
                "correlation_id": "workflow-stream-1",
            },
        )
        evidence = client.get("/v1/route/explain?correlation_id=workflow-stream-1").json()

    assert response.status_code == 200
    assert evidence["outcome_event"]["event_type"] == "chat_completion_streamed"
    assert evidence["outcome_event"]["details"]["streaming_phase"] == "completed"


@pytest.mark.asyncio
async def test_stream_adapter_explicit_close_finalizes_once_and_closes_upstream() -> None:
    upstream = CloseCountingStream([b"first", b"second"])
    events: list[dict[str, Any]] = []

    def factory(
        event_type: str,
        outcome: str,
        phase: str,
        cleanup_error: str | None,
        error_class: str | None,
    ) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "outcome": outcome,
            "phase": phase,
            "cleanup_error": cleanup_error,
            "error_class": error_class,
        }

    adapter = api._EvidenceStreamAdapter(upstream, on_terminal=events.append, event_factory=factory)
    assert await adapter.__anext__() == b"first"
    await adapter.aclose()
    await adapter.aclose()

    assert upstream.close_count == 1
    assert events == [
        {
            "event_type": "chat_completion_stream_aborted",
            "outcome": "cancelled",
            "phase": "aborted",
            "cleanup_error": None,
            "error_class": None,
        }
    ]


@pytest.mark.asyncio
async def test_stream_adapter_iterator_error_closes_upstream_and_preserves_error() -> None:
    upstream = CloseCountingStream([b"first", b"second"], error=RuntimeError("upstream failed"))
    events: list[dict[str, Any]] = []
    adapter = api._EvidenceStreamAdapter(
        upstream,
        on_terminal=events.append,
        event_factory=lambda event_type, outcome, phase, cleanup_error, error_class: {
            "event_type": event_type,
            "outcome": outcome,
            "phase": phase,
            "cleanup_error": cleanup_error,
            "error_class": error_class,
        },
    )

    assert await adapter.__anext__() == b"first"
    with pytest.raises(RuntimeError, match="upstream failed"):
        await adapter.__anext__()

    assert upstream.close_count == 1
    assert events[0]["event_type"] == "chat_completion_stream_error"
    assert events[0]["error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_stream_adapter_cancellation_closes_upstream_once() -> None:
    upstream = CloseCountingStream([], error=asyncio.CancelledError())
    events: list[dict[str, Any]] = []
    adapter = api._EvidenceStreamAdapter(
        upstream,
        on_terminal=events.append,
        event_factory=lambda event_type, outcome, phase, cleanup_error, error_class: {
            "event_type": event_type,
            "outcome": outcome,
            "phase": phase,
            "cleanup_error": cleanup_error,
            "error_class": error_class,
        },
    )

    with pytest.raises(asyncio.CancelledError):
        await adapter.__anext__()
    await adapter.aclose()

    assert upstream.close_count == 1
    assert events[0]["event_type"] == "chat_completion_stream_aborted"
    assert events[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_stream_adapter_cleanup_error_does_not_mask_iterator_error() -> None:
    upstream = CloseCountingStream(
        [b"first", b"second"],
        error=RuntimeError("upstream failed"),
        cleanup_error=OSError("close failed"),
    )
    events: list[dict[str, Any]] = []
    adapter = api._EvidenceStreamAdapter(
        upstream,
        on_terminal=events.append,
        event_factory=lambda event_type, outcome, phase, cleanup_error, error_class: {
            "event_type": event_type,
            "outcome": outcome,
            "phase": phase,
            "cleanup_error": cleanup_error,
            "error_class": error_class,
        },
    )

    assert await adapter.__anext__() == b"first"
    with pytest.raises(RuntimeError, match="upstream failed"):
        await adapter.__anext__()

    assert upstream.close_count == 1
    assert events == [
        {
            "event_type": "chat_completion_stream_error",
            "outcome": "error",
            "phase": "error",
            "cleanup_error": "OSError",
            "error_class": "RuntimeError",
        }
    ]


@pytest.mark.asyncio
async def test_streaming_response_closes_unconsumed_body_after_disconnect() -> None:
    upstream = CloseCountingStream([b"first"])
    events: list[dict[str, Any]] = []
    adapter = api._EvidenceStreamAdapter(
        upstream,
        on_terminal=events.append,
        event_factory=lambda event_type, outcome, phase, cleanup_error, error_class: {
            "event_type": event_type,
            "outcome": outcome,
            "phase": phase,
            "cleanup_error": cleanup_error,
            "error_class": error_class,
        },
    )
    response = api._EvidenceStreamingResponse(adapter, status_code=200)

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            lambda: None,
            send,
        )

    assert upstream.close_count == 1
    assert events[0]["event_type"] == "chat_completion_stream_aborted"


def test_proxy_streaming_preserves_arbitrary_upstream_chunk_boundaries(monkeypatch) -> None:
    transport = RecordingTransport(stream=True)
    _configure_test_app(monkeypatch, transport)
    payload = {
        "model": "client-requested-model",
        "messages": [{"role": "user", "content": "preserve all fields"}],
        "stream": True,
        "parallel_tool_calls": True,
    }

    with TestClient(api.app) as client:
        response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0}}]}]}\n\ndata: [DONE]\n\n'
    )
    assert transport.requests[0]["body"] == {**payload, "model": "selected-model"}


def test_proxy_rejects_oversized_and_malformed_payloads(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setenv("LLMGATE_MAX_REQUEST_BYTES", "10")

    with TestClient(api.app) as client:
        oversized = client.post("/v1/chat/completions", content=b'{"payload":"too large"}')
        malformed = client.post("/v1/chat/completions", content=b"not-json")

    assert oversized.status_code == 413
    assert malformed.status_code == 400
    assert transport.requests == []


def test_models_endpoint_forwards_upstream_catalog(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setenv("LLMGATE_MODEL_DENYLIST", "denied-model")

    with TestClient(api.app) as client:
        readiness = client.get("/ready")
        response = client.get("/v1/models")

    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    assert response.status_code == 200
    assert response.headers["x-upstream"] == "catalog"
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.json()["data"][0]["id"] == "selected-model"
    assert len(response.json()["data"]) == 1
    assert response.json()["data"][0]["verdict"] == {
        "eligible": True,
        "availability_state": "unknown",
        "capability_profile": {
            "tier": 2,
            "context": 128000,
            "tools": True,
            "structured_output": True,
            "vision": False,
            "streaming": True,
            "reasoning": False,
            "provider": "unknown",
            "model_family": "unknown",
        },
    }
