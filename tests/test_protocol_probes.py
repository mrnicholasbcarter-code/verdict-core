from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from verdict.capability_passports import CapabilityStatus, RouteIdentity
from verdict.protocol_probes import (
    CHAT_NON_STREAM_CASE,
    CHAT_STREAM_CASE,
    RESPONSES_NON_STREAM_CASE,
    RESPONSES_STREAM_CASE,
    ProtocolProbeConsentRequiredError,
    ProtocolProbePolicy,
    ProtocolProbeRunner,
    ProtocolSurface,
)

NOW = datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc)


def route(protocol: str, endpoint: str = "https://gateway.test/v1?token=secret") -> RouteIdentity:
    return RouteIdentity(
        gateway="omniroute",
        provider="fixture-provider",
        connection="fixture-account",
        endpoint=endpoint,
        protocol=protocol,
        model_id="provider/model",
        model_revision="rev-1",
    )


def chat_body() -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "OK"}}],
        "usage": {"total_tokens": 1},
    }


def responses_body() -> dict:
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
        "usage": {"total_tokens": 1},
    }


def test_chat_and_responses_use_distinct_cases_and_payloads() -> None:
    seen: list[dict] = []

    def transport(model_id, payload, timeout):
        del model_id, timeout
        seen.append(payload)
        return {
            "status_code": 200,
            "body": chat_body() if "messages" in payload else responses_body(),
        }

    runner = ProtocolProbeRunner()
    chat = runner.run(route(ProtocolSurface.CHAT), CHAT_NON_STREAM_CASE, transport, now=NOW)
    responses = runner.run(
        route(ProtocolSurface.RESPONSES), RESPONSES_NON_STREAM_CASE, transport, now=NOW
    )

    assert chat.ready and responses.ready
    assert chat.protocol != responses.protocol
    assert seen[0]["messages"] and "input" not in seen[0]
    assert seen[1]["input"] == "Return exactly: OK" and "messages" not in seen[1]


@pytest.mark.parametrize(
    ("case", "protocol", "body"),
    [
        (CHAT_NON_STREAM_CASE, ProtocolSurface.CHAT, chat_body()),
        (RESPONSES_NON_STREAM_CASE, ProtocolSurface.RESPONSES, responses_body()),
    ],
)
def test_http_success_requires_protocol_specific_shape(case, protocol, body) -> None:
    runner = ProtocolProbeRunner()
    result = runner.run(
        route(protocol), case, lambda *_: {"status_code": 200, "body": body}, now=NOW
    )
    assert result.status == "ready"
    assert result.to_capability_evidence().status is CapabilityStatus.SUPPORTED
    malformed = runner.run(
        route(protocol), case, lambda *_: {"status_code": 200, "body": {"ok": True}}, now=NOW
    )
    assert malformed.status == "malformed"
    assert malformed.to_capability_evidence().status is CapabilityStatus.UNKNOWN


@pytest.mark.parametrize(
    "case,protocol",
    [(CHAT_STREAM_CASE, ProtocolSurface.CHAT), (RESPONSES_STREAM_CASE, ProtocolSurface.RESPONSES)],
)
def test_complete_and_truncated_streams_are_distinct(case, protocol) -> None:
    if protocol == ProtocolSurface.CHAT:
        complete = [
            {"choices": [{"delta": {"content": "OK"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        truncated = [{"choices": [{"delta": {"content": "OK"}}]}]
    else:
        complete = [
            {"type": "response.output_text.delta", "delta": "OK"},
            {"type": "response.completed"},
        ]
        truncated = [{"type": "response.output_text.delta", "delta": "OK"}]

    runner = ProtocolProbeRunner()
    ready = runner.run(
        route(protocol), case, lambda *_: {"status_code": 200, "events": complete}, now=NOW
    )
    incomplete = runner.run(
        route(protocol), case, lambda *_: {"status_code": 200, "events": truncated}, now=NOW
    )
    assert ready.status == "ready" and ready.stream_complete is True
    assert incomplete.status == "truncated" and incomplete.ready is False


@pytest.mark.parametrize(
    "status,error_class",
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (408, "timeout"),
        (409, "conflict"),
        (429, "rate_limited"),
        (503, "upstream_error"),
    ],
)
def test_error_classes_are_stable_and_sanitized(status, error_class) -> None:
    result = ProtocolProbeRunner().run(
        route(ProtocolSurface.CHAT),
        CHAT_NON_STREAM_CASE,
        lambda *_: {"status_code": status, "body": {"error": "Bearer secret"}},
        now=NOW,
    )
    assert result.status == error_class
    assert result.error_class == error_class
    assert "secret" not in str(result.to_dict())


def test_cancellation_before_and_during_stream_never_reports_ready() -> None:
    cancel = Event()
    cancel.set()
    calls: list[str] = []
    runner = ProtocolProbeRunner()
    before = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_STREAM_CASE,
        lambda *args: calls.append("called"),
        now=NOW,
        cancel_event=cancel,
    )
    assert before.status == "cancelled" and calls == []

    started = Event()
    during_cancel = Event()

    def events():
        started.set()
        yield {"choices": [{"delta": {"content": "OK"}}]}
        while not during_cancel.is_set():
            time.sleep(0.005)
        yield {"choices": [{"finish_reason": "stop"}]}

    output = []
    worker = Thread(
        target=lambda: output.append(
            runner.run(
                route(ProtocolSurface.CHAT),
                CHAT_STREAM_CASE,
                lambda *_: {"status_code": 200, "events": events()},
                now=NOW,
                cancel_event=during_cancel,
            )
        )
    )
    worker.start()
    assert started.wait(1)
    during_cancel.set()
    worker.join(1)
    assert output[0].status == "cancelled" and output[0].ready is False


def test_live_consent_is_checked_before_transport_and_route_queries_are_removed() -> None:
    calls: list[str] = []
    with pytest.raises(ProtocolProbeConsentRequiredError):
        ProtocolProbeRunner().run(
            route(ProtocolSurface.CHAT),
            CHAT_NON_STREAM_CASE,
            lambda *_: calls.append("called"),
            live=True,
            now=NOW,
        )
    assert calls == []
    result = ProtocolProbeRunner().run(
        route(ProtocolSurface.CHAT),
        CHAT_NON_STREAM_CASE,
        lambda *_: {"status_code": 200, "body": chat_body()},
        now=NOW,
    )
    assert "?" not in result.route_identity.endpoint


def test_response_and_event_limits_fail_closed() -> None:
    runner = ProtocolProbeRunner(ProtocolProbePolicy(max_response_bytes=1, max_stream_events=1))
    non_stream = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_NON_STREAM_CASE,
        lambda *_: {"status_code": 200, "body": chat_body()},
        now=NOW,
    )
    assert non_stream.status == "oversized_response"
    stream = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_STREAM_CASE,
        lambda *_: {
            "status_code": 200,
            "events": [
                {"choices": [{"delta": {"content": "OK"}}]},
                {"choices": [{"finish_reason": "stop"}]},
            ],
        },
        now=NOW,
    )
    assert stream.status == "oversized_response"


def test_digest_is_deterministic_and_bound_to_protocol_case() -> None:
    runner = ProtocolProbeRunner()
    first = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_NON_STREAM_CASE,
        lambda *_: {"status_code": 200, "body": chat_body()},
        now=NOW,
    )
    second = runner.run(
        route(ProtocolSurface.CHAT),
        CHAT_NON_STREAM_CASE,
        lambda *_: {"status_code": 200, "body": chat_body()},
        now=NOW,
    )
    assert first.evidence_digest == second.evidence_digest
    assert first.evidence_digest.startswith("sha256:")
    assert first.to_capability_evidence().source.endswith(CHAT_NON_STREAM_CASE.case_id)
