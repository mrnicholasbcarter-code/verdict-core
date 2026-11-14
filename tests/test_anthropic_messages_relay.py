"""Anthropic Messages relay surface: same gates, Anthropic wire handling."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests.relay_proof_helpers import with_relay_proofs
from tests.test_proxy import FixedIntelligence, RecordingTransport, _configure_test_app
from tests.test_stream_relay_wiring import StreamTransport, _evidence
from verdict.context_inject import extract_envelope, inject_context_pack
from verdict.evidence import request_features
from verdict.models import RoutingDecision
from verdict.proxy import UpstreamProxy
from verdict.relay import protocol_for_surface

PROMPT = "compiled pack for the messages surface"
PACK_DIGEST = "sha256:" + "ab" * 32


def _messages_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "client-requested-model",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "preserve all fields"}],
    }
    body.update(extra)
    return body


def _post_messages(
    client: TestClient,
    body: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    if content is not None:
        return client.post(
            "/v1/messages",
            content=content,
            headers={"content-type": "application/json", **(headers or {})},
        )
    return client.post(
        "/v1/messages", json=body if body is not None else _messages_body(), headers=headers
    )


class _EmptyAdmission:
    def __contains__(self, route_id: object) -> bool:
        return False


class DeniedIntelligence(FixedIntelligence):
    async def route(self, task, criticality="medium", context=None, *, request_id=None):  # type: ignore[no-untyped-def]
        decision = await super().route(task, criticality, context, request_id=request_id)
        return replace(decision, decision="denied", reason="policy denied the route")


class NoProofIntelligence(FixedIntelligence):
    async def route(self, task, criticality="medium", context=None, *, request_id=None):  # type: ignore[no-untyped-def]
        decision = await super().route(task, criticality, context, request_id=request_id)
        return replace(decision, admission_proofs={})


class ChatProofIntelligence(FixedIntelligence):
    """Mint a proof that does not name the messages protocol."""

    async def route(self, task, criticality="medium", context=None, *, request_id=None):  # type: ignore[no-untyped-def]
        chat_context = dict(context or {})
        chat_context.pop("_relay_protocol", None)
        return with_relay_proofs(
            RoutingDecision(
                model="selected-model",
                provider="omniroute",
                tier=2,
                reason="chat proof",
                request_id="request-1",
                managed_backend_status="healthy",
                quality_outcome="unknown",
            ),
            chat_context,
        )


class PackIntelligence(FixedIntelligence):
    async def route(self, task, criticality="medium", context=None, *, request_id=None):  # type: ignore[no-untyped-def]
        decision = await super().route(task, criticality, context, request_id=request_id)
        receipt = dict(decision.admit_receipt or {})
        receipt.update(
            {"pack_state": "hydrated", "pack_digest": PACK_DIGEST, "task_complete": True}
        )
        return replace(
            decision,
            safety_flags=[*decision.safety_flags, "cheap_path_context_pack"],
            context_pack_prompt=PROMPT,
            admit_receipt=receipt,
        )


def _require_server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "server-token")
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "false")


def test_protocol_for_surface_names_anthropic_messages() -> None:
    assert protocol_for_surface("messages") == "anthropic.messages"
    assert protocol_for_surface("chat") == "openai.chat.completions"
    assert protocol_for_surface("responses") == "openai.responses"
    with pytest.raises(ValueError, match="unsupported relay surface"):
        protocol_for_surface("batch")


def test_route_identity_uses_messages_endpoint() -> None:
    proxy = UpstreamProxy("http://upstream.test/v1", api_key="server-secret")
    identity = proxy.route_identity("selected-model", "anthropic.messages")
    assert identity.protocol == "anthropic.messages"
    assert identity.endpoint == "http://upstream.test/v1/messages"
    chat = proxy.route_identity("selected-model", "openai.chat.completions")
    assert chat.endpoint.endswith("/chat/completions")


def test_messages_request_forwards_to_v1_messages_with_server_credentials(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    payload = _messages_body(
        system="be careful",
        criticality="low",
        request_id="strip-me",
        correlation_id="corr-messages",
        tools=[{"name": "lookup", "description": "find", "input_schema": {"type": "object"}}],
        seed=17,
    )

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/messages",
            json=payload,
            headers={"authorization": "Bearer client-secret", "x-api-key": "client-key"},
        )
        evidence = client.get(
            "/v1/route/explain?evidence_id=" + response.headers["x-verdict-evidence-id"]
        ).json()

    assert response.status_code == 200
    assert response.headers["x-verdict-model"] == "selected-model"
    forwarded = transport.requests[0]
    assert forwarded["url"] == "http://upstream.test/v1/messages"
    assert "/v1/v1/" not in forwarded["url"]
    assert forwarded["headers"]["authorization"] == "Bearer server-secret"
    assert "x-api-key" not in {name.lower() for name in forwarded["headers"]}
    assert "client-secret" not in json.dumps(forwarded["headers"])
    assert forwarded["body"]["model"] == "selected-model"
    assert forwarded["body"]["messages"] == payload["messages"]
    assert forwarded["body"]["system"] == "be careful"
    assert forwarded["body"]["tools"] == payload["tools"]
    assert forwarded["body"]["seed"] == 17
    for stripped in ("criticality", "request_id", "correlation_id"):
        assert stripped not in forwarded["body"]
    assert (
        evidence["routing_decision"]["selected_route"]["resolved_route"]["protocol"]
        == "anthropic.messages"
    )
    assert evidence["outcome_event"]["event_type"] == "messages_buffered"


@pytest.mark.parametrize(
    ("intelligence", "fragment"),
    [
        (DeniedIntelligence, "policy denied the route"),
        (NoProofIntelligence, "missing route-bound admission proof"),
        (ChatProofIntelligence, "proof scope or observation mismatch"),
    ],
)
def test_messages_fail_closed_before_any_upstream_byte(monkeypatch, intelligence, fragment) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setattr(api, "_build_intelligence", lambda: intelligence())

    with TestClient(api.app) as client:
        response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 503
    assert transport.requests == []
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    assert fragment in body["error"]["message"]


def test_empty_attempt_set_fails_closed_before_upstream(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setattr(api, "relay_admission_provider", lambda: _EmptyAdmission())

    with TestClient(api.app) as client:
        response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 503
    assert transport.requests == []
    assert response.json()["error"]["message"] == "no executable route was selected"
    assert response.json()["type"] == "error"


def test_x_api_key_authenticates_messages_and_is_never_forwarded(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    _require_server_token(monkeypatch)

    with TestClient(api.app) as client:
        accepted = client.post(
            "/v1/messages", json=_messages_body(), headers={"x-api-key": "server-token"}
        )
        chat = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "preserve all fields"}]},
            headers={"x-api-key": "server-token"},
        )

    assert accepted.status_code == 200
    assert transport.requests[0]["url"].endswith("/v1/messages")
    assert "x-api-key" not in {name.lower() for name in transport.requests[0]["headers"]}
    assert transport.requests[0]["headers"]["authorization"] == "Bearer server-secret"
    assert chat.status_code == 401
    assert chat.json() == {
        "error": {"message": "authentication required", "type": "authentication_error"}
    }
    assert len(transport.requests) == 1


def test_conflicting_messages_credentials_are_rejected(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    _require_server_token(monkeypatch)

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/messages",
            json=_messages_body(),
            headers={"authorization": "Bearer server-token", "x-api-key": "other-token"},
        )

    assert response.status_code == 403
    assert transport.requests == []
    assert response.json() == {
        "type": "error",
        "error": {"type": "authentication_error", "message": "authentication failed"},
    }


def test_messages_errors_use_the_anthropic_envelope_and_chat_does_not(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    _require_server_token(monkeypatch)

    with TestClient(api.app) as client:
        missing = client.post("/v1/messages", json=_messages_body())
        invalid = client.post(
            "/v1/messages",
            content=b"not-json",
            headers={"content-type": "application/json", "authorization": "Bearer server-token"},
        )
        chat_invalid = client.post(
            "/v1/chat/completions",
            content=b"not-json",
            headers={"content-type": "application/json", "authorization": "Bearer server-token"},
        )

    assert missing.status_code == 401
    assert missing.json() == {
        "type": "error",
        "error": {"type": "authentication_error", "message": "authentication required"},
    }
    assert invalid.status_code == 400
    assert invalid.json()["type"] == "error"
    assert invalid.json()["error"]["type"] == "invalid_request_error"
    assert chat_invalid.status_code == 400
    assert chat_invalid.json() == {
        "error": {"message": "request body must be valid JSON", "type": "invalid_request_error"}
    }
    assert "type" not in chat_invalid.json() or chat_invalid.json().get("type") != "error"
    assert transport.requests == []


def test_messages_request_too_large_uses_anthropic_type(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setenv("LLMGATE_MAX_REQUEST_BYTES", "8")

    with TestClient(api.app) as client:
        response = client.post("/v1/messages", json=_messages_body())

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "request_too_large"
    assert response.json()["type"] == "error"
    assert transport.requests == []


def _inject(payload: dict[str, Any], *, prompt: str = PROMPT):
    return inject_context_pack(
        payload,
        surface="messages",
        compiled_prompt=prompt,
        pack_state="hydrated",
        pack_digest=PACK_DIGEST,
        prompt_digest=None,
        task_complete=True,
    )


def test_messages_injection_prepends_system_and_never_rewrites_messages() -> None:
    messages = [{"role": "user", "content": "keep me"}]
    absent = {"model": "m", "messages": messages}
    forwarded, record = _inject(absent)
    assert record.injected is True
    assert record.envelope_digest is not None
    assert forwarded["system"] == PROMPT
    assert forwarded["messages"] == messages
    assert absent.get("system") is None
    assert extract_envelope(forwarded, surface="messages") == PROMPT

    as_string = {"model": "m", "system": "be careful", "messages": messages}
    forwarded, record = _inject(as_string)
    assert record.injected is True
    assert forwarded["system"] == f"{PROMPT}\n\nbe careful"
    assert forwarded["messages"] == messages
    assert as_string["system"] == "be careful"
    assert extract_envelope(forwarded, surface="messages") == f"{PROMPT}\n\nbe careful"

    blocks = [{"type": "text", "text": "original"}]
    as_blocks = {"model": "m", "system": blocks, "messages": messages}
    forwarded, record = _inject(as_blocks)
    assert record.injected is True
    assert forwarded["system"] == [{"type": "text", "text": PROMPT}, *blocks]
    assert forwarded["messages"] == messages
    assert as_blocks["system"] == blocks
    assert extract_envelope(forwarded, surface="messages") == PROMPT


def test_unsupported_system_shape_skips_without_a_claimed_digest() -> None:
    payload = {"model": "m", "system": 1, "messages": [{"role": "user", "content": "keep"}]}
    forwarded, record = _inject(payload)
    assert forwarded is payload
    assert record.injected is False
    assert record.reason == "unsupported_system_shape"
    assert record.envelope_digest is None
    assert payload["system"] == 1

    mixed = {"model": "m", "system": ["not-a-block"], "messages": payload["messages"]}
    forwarded, record = _inject(mixed)
    assert forwarded is mixed
    assert record.injected is False
    assert record.envelope_digest is None


def test_relay_injects_compiled_pack_into_the_system_field(monkeypatch) -> None:
    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setattr(api, "_build_intelligence", lambda: PackIntelligence())
    messages = [{"role": "user", "content": "preserve all fields"}]

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/messages", json=_messages_body(system="be careful", messages=messages)
        )

    assert response.status_code == 200
    forwarded = transport.requests[0]["body"]
    assert forwarded["system"] == f"{PROMPT}\n\nbe careful"
    assert forwarded["messages"] == messages
    assert response.headers["x-verdict-pack-injected"] == "true"


def test_request_features_counts_anthropic_tools_and_image_blocks() -> None:
    openai = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    assert request_features(openai)["tools"] is True
    assert request_features(openai)["tool_names"] == ["lookup"]
    assert request_features(openai)["vision"] is False

    anthropic = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see"},
                    {"type": "image", "source": {"type": "base64", "data": "aaaa"}},
                ],
            }
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    features = request_features(anthropic)
    assert features["tools"] is True
    assert features["tool_count"] == 1
    assert features["tool_names"] == ["lookup"]
    assert features["vision"] is True


def test_message_stop_stream_is_checked_success_and_bytes_pass_through(monkeypatch) -> None:
    wire = b"".join(
        [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n',
            b'event: ping\ndata: {"type":"ping"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
    )
    transport = StreamTransport([wire])
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/messages",
            json=_messages_body(stream=True),
            headers={"x-verdict-correlation-id": "msg-stop"},
        )
        event = _evidence(client, "msg-stop")

    assert response.status_code == 200
    assert response.content == wire
    assert event["event_type"] == "messages_streamed"
    assert event["outcome"] == "success"
    assert event["details"]["stream_state"] == "success"
    assert transport.body.close_count == 1


def test_truncated_messages_stream_fails_closed_without_success(monkeypatch) -> None:
    transport = StreamTransport([b'event: message_start\ndata: {"type":"message_start"}\n\n'])
    _configure_test_app(monkeypatch, transport)

    with TestClient(api.app) as client:
        client.post(
            "/v1/messages",
            json=_messages_body(stream=True),
            headers={"x-verdict-correlation-id": "msg-eof"},
        )
        event = _evidence(client, "msg-eof")

    assert event["outcome"] == "error"
    assert event["event_type"] != "messages_streamed"
    assert event["details"]["stream_state"] == "abrupt_eof"
    assert transport.body.close_count == 1
