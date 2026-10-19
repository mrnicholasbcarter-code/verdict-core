"""End-to-end SSE terminal wiring through the FastAPI relay (BOD-434/S06 phase 2)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests.test_proxy import RecordingTransport, _configure_test_app

CHAT_STANDARD = [
    b'data: {"model":"selected-model","choices":[{"delta":{"content":"hi"}}]}\n\n',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    b'data: {"choices":[],"usage":{"total_tokens":4}}\n\n',
    b"data: [DONE]\n\n",
]


class CountingSSE(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], error: BaseException | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.close_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.close_count += 1


class StreamTransport(RecordingTransport):
    def __init__(self, chunks: list[bytes], error: BaseException | None = None) -> None:
        super().__init__(stream=True)
        self.body = CountingSSE(chunks, error)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append({"url": str(request.url)})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.body)


def _post(client: TestClient, surface: str, correlation: str) -> httpx.Response:
    if surface == "responses":
        return client.post(
            "/v1/responses",
            json={"input": "preserve all fields", "stream": True},
            headers={"x-verdict-correlation-id": correlation},
        )
    return client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "preserve all fields"}],
            "stream": True,
            "correlation_id": correlation,
        },
    )


def _evidence(client: TestClient, correlation: str) -> dict[str, Any]:
    return client.get(f"/v1/route/explain?correlation_id={correlation}").json()["outcome_event"]


@pytest.mark.parametrize("chunking", ["one_chunk", "byte_at_a_time"])
def test_standard_chat_stream_is_relayed_and_checked_success(monkeypatch, chunking) -> None:
    wire = b"".join(CHAT_STANDARD)
    chunks = [wire] if chunking == "one_chunk" else [bytes([b]) for b in wire]
    transport = StreamTransport(chunks)
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app) as client:
        response = _post(client, "chat", "std-chat")
        event = _evidence(client, "std-chat")

    assert response.status_code == 200
    assert response.content == wire
    assert event["event_type"] == "chat_completion_streamed"
    assert event["outcome"] == "success"
    assert event["details"]["stream_state"] == "success"
    assert transport.body.close_count == 1


def test_chat_done_without_finish_reason_fails_closed(monkeypatch) -> None:
    transport = StreamTransport(
        [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n"]
    )
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app) as client:
        _post(client, "chat", "no-finish")
        event = _evidence(client, "no-finish")

    assert event["outcome"] == "error"
    assert event["details"]["error_class"] == "malformed_stream"
    assert event["details"]["stream_state"] == "protocol_error"
    assert transport.body.close_count == 1


def test_mid_stream_read_timeout_closes_once_is_not_success_and_is_not_retried(monkeypatch) -> None:
    transport = StreamTransport(CHAT_STANDARD[:1], error=httpx.ReadTimeout("slow"))
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app, raise_server_exceptions=False) as client:
        _post(client, "chat", "timeout")
        event = _evidence(client, "timeout")

    assert event["outcome"] == "error"
    assert event["details"]["error_class"] == "ReadTimeout"
    assert event["details"]["stream_state"] == "transport_error"
    assert transport.body.close_count == 1
    assert len([r for r in transport.requests if "chat/completions" in r["url"]]) == 1


@pytest.mark.parametrize(
    ("terminal", "outcome", "state"),
    [
        ("response.completed", "success", "success"),
        ("response.failed", "failure", "failed"),
        ("response.incomplete", "partial", "incomplete"),
    ],
)
def test_responses_terminal_event_decides_outcome(monkeypatch, terminal, outcome, state) -> None:
    transport = StreamTransport(
        [
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
            b"data: " + json.dumps({"type": terminal, "response": {}}).encode() + b"\n\n",
        ]
    )
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app) as client:
        response = _post(client, "responses", f"resp-{state}")
        event = _evidence(client, f"resp-{state}")

    assert response.status_code == 200
    assert terminal in response.text
    assert event["outcome"] == outcome
    assert event["details"]["stream_state"] == state
    assert transport.body.close_count == 1
