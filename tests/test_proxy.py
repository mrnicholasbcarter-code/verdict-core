"""Contract tests for the transparent OpenAI-compatible proxy slice."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

import llm_gate.api as api
from llm_gate.models import RoutingDecision
from llm_gate.proxy import UpstreamProxy


class ChunkedSSE(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


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
                headers={"content-type": "application/json", "x-upstream": "catalog"},
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


class FixedGate:
    def route(self, task: str, criticality: str = "medium") -> RoutingDecision:
        assert task == "preserve all fields"
        assert criticality == "medium"
        return RoutingDecision(
            model="selected-model",
            provider="omniroute",
            tier=2,
            reason="test selection",
        )


def _configure_test_app(monkeypatch, transport: RecordingTransport) -> None:
    monkeypatch.setattr(api, "Gate", lambda **_: FixedGate())
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy(
            "http://upstream.test/v1", api_key="server-secret", transport=transport
        ),
    )
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
    assert response.headers["x-llm-gate-model"] == "selected-model"
    forwarded = transport.requests[0]
    assert forwarded["headers"]["authorization"] == "Bearer server-secret"
    assert forwarded["body"] == {**payload, "model": "selected-model"}
    assert json.loads(response.content)["usage"]["total_tokens"] == 4


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
    assert response.json()["data"][0]["id"] == "selected-model"
    assert len(response.json()["data"]) == 1
    assert response.json()["data"][0]["llm_gate"] == {
        "eligible": True,
        "availability_state": "unknown",
        "capability_profile": {"tier": 2},
    }
