"""BOD-434 / S06: SSE terminal semantics, identity, close-once — isolated tests.

Phase 1: the stream state machine lives in ``verdict.stream_terminal`` and is
tested here without any HTTP transport. Phase 2 wires it into ``verdict.api``
behind the same constructor surface.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Any, ClassVar

import httpx
import pytest

from verdict.relay import (
    failure_class,
    fatal_identity_mismatch,
    retry_safety,
    retryable_exception,
    retryable_response_status,
    retryable_transport_status,
)
from verdict.stream_terminal import (
    DEFAULT_MAX_PENDING_EVENT_BYTES,
    CloseOnce,
    StreamProtocolError,
    StreamState,
    ValidatedSSEStream,
    identity_allows_proof,
    identity_proof_status,
    prime_stream,
)
from verdict.transitions import RetrySafety


class CloseCountingStream:
    """Minimal upstream: fixed chunks, counting ``aclose`` calls."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.close_count = 0

    def __aiter__(self) -> CloseCountingStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.close_count += 1


class HangingStream(CloseCountingStream):
    """Serves one chunk, then never answers."""

    def __init__(self) -> None:
        super().__init__([b'data: {"choices":[]}\n\n'])
        self._hang = asyncio.Event()

    async def __anext__(self) -> bytes:
        if self._chunks:
            return await super().__anext__()
        await self._hang.wait()
        raise StopAsyncIteration


async def drain(body: Any) -> bytes:
    collected = bytearray()
    async for chunk in body:
        collected += chunk
    return bytes(collected)


def chat_sse(newline: bytes = b"\n") -> bytes:
    """Canonical chat stream: comment, split UTF-8 delta, multiline data,
    two-choice finish, usage, then [DONE]."""
    lines = [
        ": keep-alive comment",
        "",
        'data: {"id":"chatcmpl-1","model":"agy/test-model",',
        'data: "choices":[{"index":0,"delta":{"content":"h\u00e9llo\u2192"}}]}',
        "",
        'data: {"id":"chatcmpl-1","model":"agy/test-model","choices":['
        '{"index":0,"delta":{},"finish_reason":"stop"},'
        '{"index":1,"delta":{},"finish_reason":"tool_calls"}]}',
        "",
        'data: {"id":"chatcmpl-1","model":"agy/test-model","choices":[],'
        '"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}',
        "",
        "data: [DONE]",
        "",
    ]
    return newline.join(line.encode() for line in lines)


async def test_sse_terminal_identity_and_close_once_are_chunking_independent() -> None:
    lf = chat_sse(b"\n")
    crlf = chat_sse(b"\r\n")
    split = lf.index("é".encode()) + 1  # inside the two bytes of U+00E9
    chunkings: dict[str, list[bytes]] = {
        "one-chunk": [lf],
        "byte-by-byte": [lf[i : i + 1] for i in range(len(lf))],
        "utf8-split": [lf[:split], lf[split:]],
        "crlf-one-chunk": [crlf],
        "crlf-byte-by-byte": [crlf[i : i + 1] for i in range(len(crlf))],
    }
    outcomes = []
    for name, chunks in chunkings.items():
        raw = CloseCountingStream(chunks)
        validated = ValidatedSSEStream(raw, surface="chat")
        body = await prime_stream(validated)
        passed_through = await drain(body)
        expected = crlf if name.startswith("crlf") else lf
        assert passed_through == expected
        assert raw.close_count == 1
        await body.aclose()  # idempotent repeat at the outer boundary
        await validated.aclose()  # idempotent repeat at the inner boundary
        assert raw.close_count == 1
        assert validated.outcome is not None
        outcomes.append(validated.outcome)
    reference = outcomes[0]
    for outcome in outcomes:
        assert outcome.state == reference.state == StreamState.SUCCESS
        assert outcome.checked_success
        assert outcome.finish_reasons == ("stop", "tool_calls")
        assert outcome.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
        assert outcome.served_model == "agy/test-model"
        assert outcome.terminal_event == "chat_done"
    # identity: only the matching, attested selection mints proof
    assert identity_allows_proof("agy/test-model", reference.served_model, attested=True)
    assert not identity_allows_proof("agy/other-model", reference.served_model, attested=True)
    assert not identity_allows_proof("agy/test-model", reference.served_model, attested=False)
    assert not identity_allows_proof("agy/test-model", None, attested=True)


async def test_chat_finish_usage_done_ordering_is_success() -> None:
    payload = (
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"choices":[],"usage":{"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="chat")
    await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.checked_success
    assert validated.terminal


async def test_chat_tool_call_finish_reason_is_checked_success() -> None:
    payload = (
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"id":"c","function":{"name":"f"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="chat")
    await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.checked_success
    assert validated.outcome.finish_reasons == ("tool_calls",)


async def test_chat_done_without_finish_reason_fails_closed() -> None:
    validated = ValidatedSSEStream(CloseCountingStream([b"data: [DONE]\n\n"]), surface="chat")
    with pytest.raises(StreamProtocolError):
        await drain(validated)
    assert validated.outcome is not None
    assert not validated.outcome.checked_success
    assert validated.outcome.state == StreamState.PROTOCOL_ERROR


async def test_chat_eof_after_finish_without_done_is_abrupt() -> None:
    payload = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="chat")
    with pytest.raises(StreamProtocolError):
        await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.ABRUPT_EOF
    assert not validated.outcome.checked_success


async def test_data_after_done_fails() -> None:
    payload = (
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
        b'data: {"choices":[]}\n\n'
    )
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="chat")
    with pytest.raises(StreamProtocolError):
        await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.PROTOCOL_ERROR


async def test_responses_terminal_states_map_to_outcomes() -> None:
    completed = b'data: {"type":"response.completed","usage":{"total_tokens":2}}\n\n'
    validated = ValidatedSSEStream(CloseCountingStream([completed]), surface="responses")
    await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.SUCCESS
    assert validated.outcome.terminal_event == "response.completed"
    assert validated.outcome.usage == {"total_tokens": 2}

    failed = b'data: {"type":"response.failed","response":{"error":{"code":"server_error"}}}\n\n'
    validated = ValidatedSSEStream(CloseCountingStream([failed]), surface="responses")
    await drain(validated)  # a semantic failure passes through; it never raises
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.FAILED
    assert not validated.outcome.checked_success

    incomplete = b'data: {"type":"response.incomplete","response":{"incomplete_details":{}}}\n\n'
    validated = ValidatedSSEStream(CloseCountingStream([incomplete]), surface="responses")
    await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.INCOMPLETE


async def test_responses_abrupt_eof_fails_closed() -> None:
    payload = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="responses")
    with pytest.raises(StreamProtocolError):
        await drain(validated)
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.ABRUPT_EOF


async def test_aggregate_event_overflow_always_fails() -> None:
    pad = b"x" * (DEFAULT_MAX_PENDING_EVENT_BYTES + 8)
    event = b'data: {"pad":"' + pad + b'"}\n\n'
    for chunks in ([event], [event[i : i + 1] for i in range(len(event))]):
        raw = CloseCountingStream(chunks)
        validated = ValidatedSSEStream(raw, surface="chat")
        with pytest.raises(StreamProtocolError):
            await drain(validated)
        assert validated.outcome is not None
        assert validated.outcome.state == StreamState.PROTOCOL_ERROR
        assert raw.close_count == 1


async def test_multiline_accumulation_overflow_fails() -> None:
    half = b"x" * (DEFAULT_MAX_PENDING_EVENT_BYTES // 2 + 1)
    payload = b"data: " + half + b"\ndata: " + half + b"\n\n"
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="chat")
    with pytest.raises(StreamProtocolError):
        await drain(validated)


async def test_undelivered_partial_line_overflow_fails() -> None:
    payload = b"data: " + b"x" * (DEFAULT_MAX_PENDING_EVENT_BYTES + 8)
    validated = ValidatedSSEStream(CloseCountingStream([payload]), surface="chat")
    with pytest.raises(StreamProtocolError):
        await drain(validated)


async def test_total_byte_and_event_limits_fail() -> None:
    event = b'data: {"choices":[]}\n\n'
    raw = CloseCountingStream([event, event])
    validated = ValidatedSSEStream(raw, surface="chat", max_bytes=len(event) + 1)
    with pytest.raises(StreamProtocolError):
        await drain(validated)

    raw = CloseCountingStream([event, event, event])
    validated = ValidatedSSEStream(raw, surface="chat", max_events=2)
    with pytest.raises(StreamProtocolError):
        await drain(validated)


async def test_chain_cancellation_closes_upstream_once_with_one_outcome() -> None:
    payload = chat_sse(b"\n")
    head, tail = payload[: len(payload) // 2], payload[len(payload) // 2 :]
    raw = CloseCountingStream([head[:16], head[16:], tail])
    validated = ValidatedSSEStream(raw, surface="chat")
    body = await prime_stream(validated)
    await body.__anext__()  # consume part of the stream; no terminal yet
    assert not validated.terminal
    await body.aclose()
    await body.aclose()
    await validated.aclose()
    assert raw.close_count == 1
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.ABORTED
    assert not validated.outcome.checked_success


async def test_cancelled_iteration_aborts_and_closes_once() -> None:
    raw = HangingStream()
    validated = ValidatedSSEStream(raw, surface="chat")
    body = await prime_stream(validated)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(drain(body), timeout=0.05)
    await body.aclose()
    assert raw.close_count == 1
    assert validated.outcome is not None
    assert validated.outcome.state == StreamState.ABORTED
    assert not validated.outcome.checked_success


async def test_close_once_records_close_error_and_never_recloses() -> None:
    class LeakyStream(CloseCountingStream):
        async def aclose(self) -> None:
            self.close_count += 1
            raise RuntimeError("cleanup boom")

    raw = LeakyStream([])
    once = CloseOnce(raw)
    assert await once.aclose() == "RuntimeError"
    assert await once.aclose() == "RuntimeError"
    assert raw.close_count == 1


def test_identity_e1_substitution_never_mints_proof() -> None:
    assert (
        identity_proof_status("agy/gemini-flash", "agy/gemini-pro", attested=True)
        == "identity_mismatch"
    )
    assert not identity_allows_proof("agy/gemini-flash", "agy/gemini-pro", attested=True)
    # a provider-local model id preserves the namespaced selection
    assert identity_proof_status("agy/gemini-flash", "gemini-flash", attested=True) == "verified"
    assert fatal_identity_mismatch("agy/gemini-flash", "gemini-pro")


def test_identity_e2_opaque_alias_stays_unverifiable() -> None:
    for alias in ("auto", "combo/x", "virtual/y", "router/z", "model:free"):
        assert identity_proof_status(alias, alias, attested=True) == "opaque_alias"
        assert not identity_allows_proof(alias, alias, attested=True)
        assert not identity_allows_proof(alias, "concrete/member", attested=True)


def test_identity_e3_missing_attestation_is_unavailable() -> None:
    assert identity_proof_status("agy/gemini-flash", None, attested=True) == "unavailable"
    assert identity_proof_status("agy/gemini-flash", "agy/gemini-flash", attested=False) == (
        "unavailable"
    )
    assert identity_allows_proof("agy/gemini-flash", "agy/gemini-flash", attested=True)


class _Request:
    headers: ClassVar[dict[str, str]] = {}


def test_replay_safety_membership_is_unchanged() -> None:
    # A20: auth failures are never transport-retryable
    assert not retryable_transport_status(401)
    assert not retryable_transport_status(403)
    assert failure_class(status_code=401) == "auth_failure"
    assert failure_class(status_code=403) == "auth_failure"
    assert retryable_transport_status(429)
    # tool-bearing requests are never replay-safe: with an idempotency key
    # they are explicitly UNSAFE, without one the safety is UNKNOWN and never
    # authorizes a second POST.
    tools: list[dict[str, Any]] = [{"type": "function", "function": {"name": "f"}}]
    assert retry_safety(_Request(), {"tools": tools}, "idem-key") == RetrySafety.UNSAFE
    assert retry_safety(_Request(), {"tools": tools}, None) == RetrySafety.UNKNOWN
    assert retry_safety(_Request(), {}, "idem-key") == RetrySafety.SAFE
    # timeouts stay retryable; a compatibility 4xx never replays on the same route
    assert retryable_exception(httpx.TimeoutException("t"))
    assert not retryable_response_status(400, compatibility_applied=True, body="")


def test_stream_state_is_a_closed_vocabulary() -> None:
    assert issubclass(StreamState, enum.Enum)
    assert {state.value for state in StreamState} == {
        "success",
        "failed",
        "incomplete",
        "aborted",
        "protocol_error",
        "abrupt_eof",
    }
