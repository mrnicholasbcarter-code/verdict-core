from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from verdict.capability_passports import CapabilityStatus, RouteIdentity
from verdict.protocol_probes import ProtocolSurface
from verdict.structured_qualification import (
    CHAT_STRICT_OUTPUT_CASE,
    RESPONSES_STRICT_OUTPUT_CASE,
    StructuredOutputRunner,
    StructuredQualificationConsentRequiredError,
)

NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)


def route(protocol: str) -> RouteIdentity:
    return RouteIdentity(
        "gateway",
        "provider",
        "account",
        "https://user:secret@example.test/v1?key=secret",
        protocol,
        "provider/model",
        "rev-1",
    )


def body(protocol: str, value: object) -> dict:
    encoded = json.dumps(value)
    if protocol == ProtocolSurface.CHAT:
        return {"choices": [{"message": {"role": "assistant", "content": encoded}}]}
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": encoded}]}]}


@pytest.mark.parametrize(
    ("case", "protocol"),
    [
        (CHAT_STRICT_OUTPUT_CASE, ProtocolSurface.CHAT),
        (RESPONSES_STRICT_OUTPUT_CASE, ProtocolSurface.RESPONSES),
    ],
)
def test_chat_and_responses_strict_output_are_independent(case, protocol) -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return {"status_code": 200, "body": body(protocol, {"status": "ok", "value": "done"})}

    result = StructuredOutputRunner().run(route(protocol), case, transport, now=NOW)
    assert result.ready
    assert result.to_capability_evidence().status is CapabilityStatus.SUPPORTED
    assert "?" not in result.route_identity.endpoint
    assert "secret" not in result.route_identity.endpoint
    assert ("response_format" in seen[0]) != ("text" in seen[0])
    assert seen[0].get("max_tokens", seen[0].get("max_output_tokens")) == 64


@pytest.mark.parametrize(
    ("case", "protocol"),
    [
        (CHAT_STRICT_OUTPUT_CASE, ProtocolSurface.CHAT),
        (RESPONSES_STRICT_OUTPUT_CASE, ProtocolSurface.RESPONSES),
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [
        {"status": "ok"},
        {"status": "ok", "value": 1},
        {"status": "ok", "value": "done", "extra": True},
        {"status": "bad", "value": "done"},
    ],
)
def test_http_success_does_not_imply_strict_schema_support(invalid, case, protocol) -> None:
    result = StructuredOutputRunner().run(
        route(protocol),
        case,
        lambda *_: {"status_code": 200, "body": body(protocol, invalid)},
        now=NOW,
    )
    assert result.status == "schema_invalid"
    assert not result.ready
    assert result.to_capability_evidence().status is CapabilityStatus.UNKNOWN


@pytest.mark.parametrize(
    ("case", "protocol"),
    [
        (CHAT_STRICT_OUTPUT_CASE, ProtocolSurface.CHAT),
        (RESPONSES_STRICT_OUTPUT_CASE, ProtocolSurface.RESPONSES),
    ],
)
def test_invalid_structured_fields_are_rejected_on_both_surfaces(case, protocol) -> None:
    result = StructuredOutputRunner().run(
        route(protocol),
        case,
        lambda *_: {"status_code": 200, "body": body(protocol, {"status": "bad"})},
        now=NOW,
    )
    assert result.status == "schema_invalid"
    assert result.to_capability_evidence().status is CapabilityStatus.UNKNOWN


def test_nonstandard_json_constants_and_malformed_responses_fail_closed() -> None:
    nan_body = {
        "choices": [{"message": {"role": "assistant", "content": '{"status":"ok","value":NaN}'}}]
    }
    nan = StructuredOutputRunner().run(
        route(ProtocolSurface.CHAT),
        CHAT_STRICT_OUTPUT_CASE,
        lambda *_: {"status_code": 200, "body": nan_body},
        now=NOW,
    )
    malformed = StructuredOutputRunner().run(
        route(ProtocolSurface.RESPONSES),
        RESPONSES_STRICT_OUTPUT_CASE,
        lambda *_: {"status_code": 200, "body": {"output": "not-a-list"}},
        now=NOW,
    )
    assert nan.status == nan.error_class == "schema_invalid"
    assert malformed.status == "malformed"
    assert malformed.error_class == "malformed_response"


def test_malformed_and_oversized_responses_fail_closed() -> None:
    runner = StructuredOutputRunner(max_response_bytes=20)
    malformed = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_STRICT_OUTPUT_CASE,
        lambda *_: {"status_code": 200, "body": {"ok": True}},
        now=NOW,
    )
    oversized = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_STRICT_OUTPUT_CASE,
        lambda *_: {
            "status_code": 200,
            "body": body(ProtocolSurface.CHAT, {"status": "ok", "value": "done"}),
        },
        now=NOW,
    )
    assert malformed.status == "malformed"
    assert oversized.status == "oversized_response"
    assert oversized.response_bytes > runner.max_response_bytes


def test_structured_cancellation_is_non_ready_and_skips_transport() -> None:
    cancel = Event()
    cancel.set()
    calls = []
    result = StructuredOutputRunner().run(
        route(ProtocolSurface.CHAT),
        CHAT_STRICT_OUTPUT_CASE,
        lambda *_: calls.append(True),
        now=NOW,
        cancel_event=cancel,
    )
    assert result.status == "cancelled"
    assert result.error_class == "cancelled"
    assert not result.ready and calls == []


def test_structured_timeout_and_malformed_transport_errors_are_distinct() -> None:
    timed_out = StructuredOutputRunner(timeout_seconds=0.001).run(
        route(ProtocolSurface.CHAT), CHAT_STRICT_OUTPUT_CASE, lambda *_: time.sleep(0.01), now=NOW
    )
    malformed = StructuredOutputRunner().run(
        route(ProtocolSurface.CHAT),
        CHAT_STRICT_OUTPUT_CASE,
        lambda *_: {"status_code": 200, "body": "not-json"},
        now=NOW,
    )
    assert timed_out.status == timed_out.error_class == "timeout"
    assert malformed.status == "malformed"
    assert malformed.error_class == "malformed_response"


def test_structured_cancellation_during_blocking_transport_is_bounded() -> None:
    cancel = Event()
    started = Event()

    def transport(*_):
        started.set()
        while not cancel.is_set():
            time.sleep(0.005)
        return {
            "status_code": 200,
            "body": body(ProtocolSurface.CHAT, {"status": "ok", "value": "done"}),
        }

    output = []
    worker = Thread(
        target=lambda: output.append(
            StructuredOutputRunner(timeout_seconds=0.2).run(
                route(ProtocolSurface.CHAT),
                CHAT_STRICT_OUTPUT_CASE,
                transport,
                now=NOW,
                cancel_event=cancel,
            )
        )
    )
    worker.start()
    assert started.wait(1)
    cancel.set()
    worker.join(1)
    assert output[0].status == output[0].error_class == "cancelled"


def test_live_consent_checked_before_transport_and_digest_is_deterministic() -> None:
    calls = []
    with pytest.raises(StructuredQualificationConsentRequiredError):
        StructuredOutputRunner().run(
            route(ProtocolSurface.CHAT),
            CHAT_STRICT_OUTPUT_CASE,
            lambda *_: calls.append(True),
            live=True,
            now=NOW,
        )
    assert not calls

    def transport(*_):
        return {
            "status_code": 200,
            "body": body(ProtocolSurface.CHAT, {"status": "ok", "value": "done"}),
        }

    first = StructuredOutputRunner().run(
        route(ProtocolSurface.CHAT), CHAT_STRICT_OUTPUT_CASE, transport, now=NOW
    )
    second = StructuredOutputRunner().run(
        route(ProtocolSurface.CHAT), CHAT_STRICT_OUTPUT_CASE, transport, now=NOW
    )
    assert first.evidence_digest == second.evidence_digest
    assert "secret" not in str(first.to_dict())
