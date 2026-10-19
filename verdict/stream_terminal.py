"""SSE terminal semantics, served-identity gates, and close-once stream wrappers.

BOD-434 / S06 (acceptance A17-A23). The HTTP relay surfaces need one place
that owns, independent of transport and chunk boundaries:

* incremental SSE framing: CRLF, comment lines, multiline ``data:`` fields and
  UTF-8 sequences split across chunks all parse identically (A18);
* semantic terminal status tracked separately from transport EOF: chat
  ``finish_reason -> usage -> [DONE]`` (tool-call turns included) is the only
  shape that becomes checked success; failed/incomplete Responses and abrupt
  EOF never do (A17);
* accumulated bounds: 16 MiB stream, 256 KiB pending aggregate event,
  100 000 events, and overflow always fails (A18);
* served-identity policy E1/E2/E3 extracted from ``verdict.relay``: a
  substituted concrete model, an opaque alias, or a missing attestation can
  never mint proof (A20/A21);
* close-once cleanup: every wrapper's ``aclose`` is idempotent and the real
  upstream closes exactly once with exactly one terminal outcome (A19).

``verdict.api`` wires these primitives thinly (phase 2); nothing here imports
the HTTP layer, so the whole contract is testable without transport.
"""

from __future__ import annotations

import asyncio
import codecs
import enum
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, NoReturn

from verdict.relay import fatal_identity_mismatch, is_opaque_alias

DEFAULT_MAX_STREAM_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PENDING_EVENT_BYTES = 256 * 1024
DEFAULT_MAX_EVENTS = 100_000

_RESPONSES_TERMINAL_TYPES = frozenset(
    {"response.completed", "response.failed", "response.incomplete"}
)


class StreamProtocolError(RuntimeError):
    """Raised when an upstream stream is malformed or ends without a terminal event."""

    code = "malformed_stream"


class StreamState(str, enum.Enum):
    """Closed vocabulary for the one terminal outcome of a stream."""

    SUCCESS = "success"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    ABORTED = "aborted"
    PROTOCOL_ERROR = "protocol_error"
    ABRUPT_EOF = "abrupt_eof"


@dataclass(frozen=True)
class StreamOutcome:
    """The single terminal outcome a stream produces, however it ends."""

    state: StreamState
    surface: str
    finish_reasons: tuple[str, ...] = ()
    usage: dict[str, Any] | None = None
    terminal_event: str | None = None
    served_model: str | None = None
    detail: str | None = None
    cleanup_error: str | None = None

    @property
    def checked_success(self) -> bool:
        """True only for a semantically complete, successful terminal."""

        return self.state is StreamState.SUCCESS


class SSETerminalParser:
    """Chunk-boundary-independent SSE framing plus terminal-state tracking.

    Feeding the same bytes as one chunk, byte-by-byte, or with multibyte UTF-8
    sequences split across chunk edges produces identical results. Bytes are
    never re-emitted here; callers yield the original chunks unchanged.
    """

    def __init__(
        self,
        *,
        surface: str,
        max_bytes: int = DEFAULT_MAX_STREAM_BYTES,
        max_buffer: int = DEFAULT_MAX_PENDING_EVENT_BYTES,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        if surface not in {"chat", "responses"}:
            raise ValueError(f"unsupported relay surface: {surface}")
        self._surface = surface
        self._max_bytes = max_bytes
        self._max_buffer = max_buffer
        self._max_events = max_events
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._event_data: list[str] = []
        self._pending = 0
        self._bytes = 0
        self._events = 0
        self._done = False
        self._finish_reasons: list[str] = []
        self._usage: dict[str, Any] | None = None
        self._models: list[str] = []
        self._responses_terminal: str | None = None
        self._outcome: StreamOutcome | None = None

    @property
    def surface(self) -> str:
        return self._surface

    @property
    def outcome(self) -> StreamOutcome | None:
        """The terminal outcome, set exactly once, or ``None`` while open."""

        return self._outcome

    @property
    def terminal(self) -> bool:
        """Semantic terminal seen, independent of transport EOF."""

        if self._surface == "chat":
            return self._done
        return self._responses_terminal is not None

    def feed_chunk(self, chunk: bytes) -> None:
        if self._outcome is not None:
            self._fail("stream already reached its terminal outcome")
        if not isinstance(chunk, bytes):
            self._fail("upstream stream yielded a non-byte chunk")
        self._bytes += len(chunk)
        if self._bytes > self._max_bytes:
            self._fail("stream exceeded the aggregate byte limit")
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self._fail("stream contained invalid UTF-8")
        self._feed_text(text)

    def finalize(self) -> StreamOutcome:
        """Consume transport EOF and return the one terminal outcome.

        Protocol violations and EOF without a semantic terminal raise
        :class:`StreamProtocolError` after the outcome is recorded, so the
        outcome is always observable even when finalization fails.
        """

        if self._outcome is not None:
            return self._outcome
        try:
            text = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            self._fail("stream contained invalid UTF-8")
        if text:
            self._feed_text(text)
        if self._event_data:
            self._finish_event()
        if self._surface == "chat":
            state = StreamState.SUCCESS if self._done else StreamState.ABRUPT_EOF
            detail = None if self._done else "stream ended without a terminal event"
            terminal_event = "chat_done" if self._done else None
        else:
            if self._responses_terminal == "response.completed":
                state = StreamState.SUCCESS
            elif self._responses_terminal == "response.failed":
                state = StreamState.FAILED
            elif self._responses_terminal == "response.incomplete":
                state = StreamState.INCOMPLETE
            else:
                state = StreamState.ABRUPT_EOF
            detail = None if self._responses_terminal else "stream ended without a terminal event"
            terminal_event = self._responses_terminal
        self._outcome = StreamOutcome(
            state=state,
            surface=self._surface,
            finish_reasons=tuple(self._finish_reasons),
            usage=self._usage,
            terminal_event=terminal_event,
            served_model=self._models[-1] if self._models else None,
            detail=detail,
        )
        if detail is not None:
            raise StreamProtocolError(detail)
        return self._outcome

    def _feed_text(self, text: str) -> None:
        self._buffer += text
        if len(self._buffer) > self._max_buffer:
            self._fail("stream event buffer exceeded its limit")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if len(self._buffer) > self._max_buffer:
                self._fail("stream event buffer exceeded its limit")
            self._consume_line(line.rstrip("\r"))

    def _consume_line(self, line: str) -> None:
        if line == "":
            if self._event_data:
                self._finish_event()
            return
        if line.startswith(":"):
            return
        if line.startswith("data:"):
            value = line[5:].lstrip()
            if self._pending + len(value) > self._max_buffer:
                self._fail("stream aggregate event exceeded its limit")
            self._event_data.append(value)
            self._pending += len(value)
            return
        # ``event:``, ``id:`` and ``retry:`` are valid SSE metadata. Unknown
        # fields are ignored by the SSE standard and stay wire-transparent.

    def _finish_event(self) -> None:
        data = "\n".join(self._event_data)
        self._event_data.clear()
        self._pending = 0
        self._events += 1
        if self._events > self._max_events:
            self._fail("stream exceeded its event limit")
        if self._done or self._responses_terminal is not None:
            self._fail("stream emitted data after its terminal event")
        if self._surface == "chat" and data == "[DONE]":
            self._done = True
            if not self._finish_reasons:
                self._fail("chat stream ended with [DONE] but no finish_reason")
            return
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._fail("stream event was not valid JSON")
        if not isinstance(parsed, dict):
            self._fail("stream event must be a JSON object")
        if self._surface == "responses":
            event_type = parsed.get("type")
            if event_type in _RESPONSES_TERMINAL_TYPES:
                self._responses_terminal = str(event_type)
                usage = parsed.get("usage")
                if isinstance(usage, dict):
                    self._usage = usage
            return
        model = parsed.get("model")
        if isinstance(model, str) and model.strip():
            self._models.append(model)
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        choices = parsed.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                    self._finish_reasons.append(str(choice["finish_reason"]))

    def _fail(self, message: str) -> NoReturn:
        if self._outcome is None:
            self._outcome = StreamOutcome(
                state=StreamState.PROTOCOL_ERROR,
                surface=self._surface,
                finish_reasons=tuple(self._finish_reasons),
                usage=self._usage,
                served_model=self._models[-1] if self._models else None,
                detail=message,
            )
        raise StreamProtocolError(message)


class CloseOnce:
    """Call the wrapped object's ``aclose`` exactly once, absorbing repeats.

    A repeated (or concurrent) close is a no-op. A close error is recorded and
    returned, never re-raised: cleanup errors are terminal-outcome data, not a
    reason to abandon the outcome that is already recorded.
    """

    def __init__(self, upstream: Any) -> None:
        self._upstream = upstream
        self._closed = False
        self._error: str | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_error(self) -> str | None:
        return self._error

    async def aclose(self) -> str | None:
        if self._closed:
            return self._error
        self._closed = True
        close = getattr(self._upstream, "aclose", None)
        if not callable(close):
            return None
        try:
            await close()
        except (Exception, asyncio.CancelledError) as exc:
            self._error = type(exc).__name__
        return self._error


class PrimedBody:
    """A first-chunk-primed body that always closes its source exactly once.

    The first chunk is buffered (the relay commits to a client stream only
    after it), the rest streams straight through. Because there is no
    generator between this wrapper and the validated stream, ``aclose`` always
    reaches the source even if the body was never iterated.
    """

    def __init__(self, body: AsyncIterator[bytes], first: bytes) -> None:
        self._body = body
        self._first = first
        self._first_pending = True

    def __aiter__(self) -> PrimedBody:
        return self

    async def __anext__(self) -> bytes:
        if self._first_pending:
            self._first_pending = False
            return self._first
        return await self._body.__anext__()

    async def aclose(self) -> None:
        close = getattr(self._body, "aclose", None)
        if callable(close):
            await close()


async def prime_stream(body: AsyncIterator[bytes]) -> PrimedBody:
    """Read the first chunk before the stream is committed to a consumer.

    Priming never opens a second upstream: the validated stream stays the sole
    owner of the transport, and an empty stream closes it before failing.
    """

    try:
        first = await body.__anext__()
    except StopAsyncIteration as exc:
        close = getattr(body, "aclose", None)
        if callable(close):
            await close()
        raise StreamProtocolError("stream ended before its first byte") from exc
    return PrimedBody(body, first)


class ValidatedSSEStream:
    """Validate SSE framing while yielding the original bytes unchanged.

    The stream owns one :class:`SSETerminalParser` and one :class:`CloseOnce`.
    However it ends — completion, protocol violation, cancellation or explicit
    close — it records exactly one :class:`StreamOutcome` and closes the real
    upstream exactly once. The semantic terminal (:attr:`terminal`) is
    independent of transport EOF; only a successful semantic terminal makes
    :attr:`outcome`.``checked_success`` true.
    """

    def __init__(
        self,
        upstream: AsyncIterator[bytes],
        *,
        surface: str,
        max_bytes: int = DEFAULT_MAX_STREAM_BYTES,
        max_buffer: int = DEFAULT_MAX_PENDING_EVENT_BYTES,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self._upstream = upstream
        self._parser = SSETerminalParser(
            surface=surface, max_bytes=max_bytes, max_buffer=max_buffer, max_events=max_events
        )
        self._closer = CloseOnce(upstream)
        self._closed = False
        self._abort_outcome: StreamOutcome | None = None

    def __aiter__(self) -> ValidatedSSEStream:
        return self

    async def __anext__(self) -> bytes:
        try:
            chunk = await self._upstream.__anext__()
        except StopAsyncIteration:
            try:
                self._parser.finalize()
            except StreamProtocolError:
                await self._closer.aclose()
                raise
            await self._closer.aclose()
            raise
        except asyncio.CancelledError:
            await self._abort()
            raise
        try:
            self._parser.feed_chunk(chunk)
        except StreamProtocolError:
            await self._closer.aclose()
            raise
        return chunk

    @property
    def terminal(self) -> bool:
        """Semantic terminal seen (chat ``[DONE]`` / Responses terminal event)."""

        return self._parser.terminal

    @property
    def outcome(self) -> StreamOutcome | None:
        if self._abort_outcome is not None:
            return self._abort_outcome
        return self._parser.outcome

    async def aclose(self) -> None:
        """Close the real upstream exactly once and record one abort outcome.

        Idempotent: after a completed stream the recorded outcome (success,
        failed, incomplete, protocol error, abrupt EOF) is preserved; only a
        still-open stream records an abort.
        """

        if self._closed:
            return
        self._closed = True
        await self._abort()

    async def _abort(self) -> None:
        cleanup_error = await self._closer.aclose()
        if self._abort_outcome is None and self._parser.outcome is None:
            self._abort_outcome = StreamOutcome(
                state=StreamState.ABORTED, surface=self._parser.surface, cleanup_error=cleanup_error
            )


def identity_proof_status(selected: str, served: str | None, *, attested: bool) -> str:
    """Normalize an attempt's served identity into a proof status (E1/E2/E3).

    E1 — substitution: a concrete selected model that a gateway served as a
    different model can never mint proof (``fatal_identity_mismatch``); a
    provider-local id for the namespaced selection preserves identity.
    E2 — opacity: an opaque alias never attests a concrete member route, so
    it stays unverifiable and protected work fails closed on it.
    E3 — attestation: without a served identity or an adapter attestation the
    receipt can only record ``"unavailable"``; it never becomes verified.
    """

    if is_opaque_alias(selected):
        return "opaque_alias"
    if fatal_identity_mismatch(selected, served):
        return "identity_mismatch"
    if served is None or not attested:
        return "unavailable"
    return "verified"


def identity_allows_proof(selected: str, served: str | None, *, attested: bool) -> bool:
    """True only when the served identity can mint proof for the selection."""

    return identity_proof_status(selected, served, attested=attested) == "verified"


__all__ = [
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_MAX_PENDING_EVENT_BYTES",
    "DEFAULT_MAX_STREAM_BYTES",
    "CloseOnce",
    "PrimedBody",
    "SSETerminalParser",
    "StreamOutcome",
    "StreamProtocolError",
    "StreamState",
    "ValidatedSSEStream",
    "identity_allows_proof",
    "identity_proof_status",
    "prime_stream",
]
