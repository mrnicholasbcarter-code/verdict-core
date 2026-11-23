"""Relay treats HTTP 200 / finish_reason 'length' / empty content as a failed
attempt and continues to the next admitted candidate.

Shape taken from a live reproduction (2026-11-23): the relay sent a ~18 k-token
context pack to antigravity/gemini-3.5-flash-lite with max_tokens=16; the model
consumed all 13 output tokens on internal reasoning and returned
finish_reason "length" with empty content.  Every test here uses stub
transports; nothing calls a provider.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests import test_serve_failure_feedback as feedback
from verdict.orchestration.health_cache import HealthCache, default_cache_path
from verdict.orchestration.provider_catalog import is_truncated_empty_reply

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _truncated_body(model: str) -> dict[str, Any]:
    """HTTP 200 with finish_reason 'length' and empty content.

    Reflects the exact shape returned by antigravity/gemini-3.5-flash-lite
    when the context pack consumed the full output budget (2026-11-23 reproduction).
    """
    return {
        "id": "chatcmpl-trunc",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": ""}}
        ],
        "usage": {
            "prompt_tokens": 18561,
            "completion_tokens": 13,
            "total_tokens": 18574,
            "completion_tokens_details": {"reasoning_tokens": 13},
        },
    }


def _answer_body(model: str, content: str = "OK") -> dict[str, Any]:
    return {
        "id": "chatcmpl-answer",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101},
    }


# ---------------------------------------------------------------------------
# Detector unit tests
# ---------------------------------------------------------------------------


def test_detector_flags_empty_string_content_with_length_finish_reason() -> None:
    assert is_truncated_empty_reply(_truncated_body("m"))
    assert is_truncated_empty_reply(json.dumps(_truncated_body("m")).encode())


def test_detector_flags_null_content_with_length_finish_reason() -> None:
    body = _truncated_body("m")
    body["choices"][0]["message"]["content"] = None
    assert is_truncated_empty_reply(body)


def test_detector_ignores_partial_answer_with_non_empty_content() -> None:
    """(b) finish_reason 'length' + non-empty content = partial answer, not truncated."""
    body = _truncated_body("m")
    body["choices"][0]["message"]["content"] = "Here is a partial answer"
    assert not is_truncated_empty_reply(body)


def test_detector_ignores_tool_call_with_no_content() -> None:
    """(c) tool_calls with absent content is a valid function-call response."""
    body: dict[str, Any] = {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"type": "function", "function": {"name": "read", "arguments": "{}"}}
                    ],
                },
            }
        ],
    }
    assert not is_truncated_empty_reply(body)


def test_detector_ignores_finish_reason_stop_with_empty_content() -> None:
    body = _truncated_body("m")
    body["choices"][0]["finish_reason"] = "stop"
    assert not is_truncated_empty_reply(body)


def test_detector_ignores_missing_or_bad_input() -> None:
    assert not is_truncated_empty_reply(b"not json")
    assert not is_truncated_empty_reply(None)
    assert not is_truncated_empty_reply({})
    assert not is_truncated_empty_reply(b"")


# ---------------------------------------------------------------------------
# Relay integration tests
# ---------------------------------------------------------------------------


def test_relay_fails_over_from_truncated_empty_reply_no_health_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) Empty-length first candidate → relay continues to the second.

    Verifies:
    - Both upstream models are tried (len(sent) == 2).
    - The second candidate's answer reaches the client.
    - The first attempt is recorded with failure_class 'truncated_empty_reply'
      and outcome 'error'.
    - No route health negative is written (truncated_empty_reply is not in
      _RELAY_FEEDBACK_CATEGORIES; this is a prompt-budget event, not a route
      availability event).
    """
    sent: list[str] = []
    captured_events: list[Any] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        if len(sent) == 1:
            return httpx.Response(200, json=_truncated_body(model))
        return httpx.Response(200, json=_answer_body(model))

    # Spy on build_outcome_event (a module-level name in verdict.api) to
    # capture the final attempted_routes list without touching the internals.
    original_build = api.build_outcome_event

    def capturing_build(*args: Any, **kwargs: Any) -> Any:
        event = original_build(*args, **kwargs)
        captured_events.append(event)
        return event

    monkeypatch.setattr(api, "build_outcome_event", capturing_build)
    prepared = feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=feedback._request_body(),
            headers={"idempotency-key": "truncated-failover"},
        )

    assert len(sent) == 2 and sent[0] != sent[1], (
        "truncated empty must count as a failed attempt and trigger failover"
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "OK"
    assert response.headers["x-verdict-model"] == sent[1]

    # Verify the attempts are recorded with the right failure class.
    buffered = next(
        (e for e in reversed(captured_events) if e.event_type == "chat_completion_buffered"), None
    )
    assert buffered is not None, "must have a chat_completion_buffered event"
    attempted = (buffered.details or {}).get("attempted_routes", [])
    assert len(attempted) == 2, "both attempts must appear in attempted_routes"
    assert attempted[0]["outcome"] == "error"
    assert attempted[0]["failure_class"] == "truncated_empty_reply"
    assert attempted[1]["outcome"] == "success"
    assert attempted[1]["failure_class"] is None

    # No health negative: truncated_empty_reply is not a route health event.
    cache = HealthCache(default_cache_path())
    assert cache.entry(feedback._attempted_route(prepared)) is None, (
        "a truncated-empty reply must not write a route health negative"
    )


def test_relay_returns_partial_answer_with_length_finish_reason_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) finish_reason 'length' with non-empty content is a partial answer and
    passes through without triggering failover."""
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        body = _truncated_body(model)
        body["choices"][0]["message"]["content"] = "Here is a partial answer"
        return httpx.Response(200, json=body)

    feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=feedback._request_body(),
            headers={"idempotency-key": "partial-pass"},
        )
    # Only one attempt: relay did not treat the partial answer as a failed attempt.
    assert len(sent) == 1
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert content == "Here is a partial answer"


def test_relay_passes_tool_call_with_no_text_content_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A tool call with no text content is a valid response and must not
    trigger failover."""
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-tool",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )

    feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=feedback._request_body(),
            headers={"idempotency-key": "tool-call-pass"},
        )
    assert len(sent) == 1, "a tool call response must not trigger failover"
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


def test_relay_bounded_when_all_candidates_return_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) All candidates return truncated_empty → relay is bounded; the client
    receives a named error response, not a 200 with empty content and not a hang.
    """
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        return httpx.Response(200, json=_truncated_body(model))

    feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=feedback._request_body(),
            headers={"idempotency-key": "all-truncated"},
        )
    # Both admitted candidates were tried and all were exhausted.
    assert len(sent) == 2
    # The client receives an error, not a 200 with empty content.
    assert response.status_code >= 400
    body = response.json()
    # The error carries the named failure code, not a generic message.
    error = body.get("error", {})
    assert error.get("code") == "truncated_empty_reply"
