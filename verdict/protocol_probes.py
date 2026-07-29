"""Hermetic protocol-surface qualification for exact executable routes.

The catalog and the bounded liveness probe deliberately do not prove that an
OpenAI-compatible route implements either protocol surface.  This module
adds small, injected-transport cases for Chat Completions and Responses while
keeping stream completion, cancellation, error classes, and evidence
separate.  It never stores an upstream payload or creates a live transport.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, Protocol

from verdict.capability_passports import CapabilityEvidence, CapabilityStatus, RouteIdentity

PROTOCOL_PROBE_VERSION = "1"
PROTOCOL_PROBE_PROMPT = "Return exactly: OK"


class ProtocolProbeConsentRequiredError(ValueError):
    """Raised before an injected transport is invoked without live consent."""


class ProtocolProbeTransport(Protocol):
    """Injected transport used by hermetic protocol qualification cases."""

    def __call__(
        self, model_id: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> Any: ...


class ProtocolSurface:
    """Stable protocol identifiers used in route identity and evidence."""

    CHAT = "openai.chat.completions"
    RESPONSES = "openai.responses"


@dataclass(frozen=True)
class ProtocolProbeCase:
    """One independently qualified protocol operation."""

    case_id: str
    protocol: str
    stream: bool
    version: str = PROTOCOL_PROBE_VERSION

    def __post_init__(self) -> None:
        if self.protocol not in {ProtocolSurface.CHAT, ProtocolSurface.RESPONSES}:
            raise ValueError("unsupported protocol surface")
        if not self.case_id.strip():
            raise ValueError("case_id is required")

    @property
    def capability(self) -> str:
        return "chat.completions" if self.protocol == ProtocolSurface.CHAT else "responses"


CHAT_NON_STREAM_CASE = ProtocolProbeCase(
    "chat-completions-non-stream-v1", ProtocolSurface.CHAT, stream=False
)
CHAT_STREAM_CASE = ProtocolProbeCase(
    "chat-completions-stream-v1", ProtocolSurface.CHAT, stream=True
)
RESPONSES_NON_STREAM_CASE = ProtocolProbeCase(
    "responses-non-stream-v1", ProtocolSurface.RESPONSES, stream=False
)
RESPONSES_STREAM_CASE = ProtocolProbeCase(
    "responses-stream-v1", ProtocolSurface.RESPONSES, stream=True
)
PROTOCOL_PROBE_CASES = (
    CHAT_NON_STREAM_CASE,
    CHAT_STREAM_CASE,
    RESPONSES_NON_STREAM_CASE,
    RESPONSES_STREAM_CASE,
)


@dataclass(frozen=True)
class ProtocolProbePolicy:
    """Read, event, and freshness bounds for one protocol case."""

    timeout_seconds: float = 20.0
    max_response_bytes: int = 1_048_576
    max_stream_events: int = 128
    evidence_ttl_seconds: int = 900

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.max_stream_events < 1:
            raise ValueError("max_stream_events must be positive")
        if self.evidence_ttl_seconds < 1:
            raise ValueError("evidence_ttl_seconds must be positive")


@dataclass(frozen=True)
class ProtocolProbeObservation:
    """Sanitized protocol result suitable for capability evidence."""

    route_identity: RouteIdentity
    case_id: str
    protocol: str
    stream: bool
    status: str
    observed_at: datetime
    expires_at: datetime
    confidence: float
    evidence_digest: str
    limitations: tuple[str, ...] = ()
    http_status: int | None = None
    error_class: str | None = None
    response_bytes: int = 0
    event_count: int = 0
    stream_complete: bool | None = None
    usage_available: bool = False
    reasoning_fields_present: bool = False

    @property
    def ready(self) -> bool:
        """Only a fully validated protocol response is ready."""

        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        """Return a stable, payload-free diagnostic projection."""

        return {
            "protocol_probe_version": PROTOCOL_PROBE_VERSION,
            "route_identity": self.route_identity.to_dict(),
            "case_id": self.case_id,
            "protocol": self.protocol,
            "stream": self.stream,
            "status": self.status,
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at),
            "confidence": self.confidence,
            "evidence_digest": self.evidence_digest,
            "limitations": list(self.limitations),
            "http_status": self.http_status,
            "error_class": self.error_class,
            "response_bytes": self.response_bytes,
            "event_count": self.event_count,
            "stream_complete": self.stream_complete,
            "usage_available": self.usage_available,
            "reasoning_fields_present": self.reasoning_fields_present,
        }

    def to_capability_evidence(self) -> CapabilityEvidence:
        """Convert the result into the shared fail-closed passport contract."""

        return CapabilityEvidence(
            status=CapabilityStatus.SUPPORTED if self.ready else CapabilityStatus.UNKNOWN,
            source=f"verdict:protocol-probe/{self.case_id}",
            observed_at=self.observed_at,
            expires_at=self.expires_at,
            confidence=self.confidence,
            evidence_digest=self.evidence_digest,
            limitations=self.limitations,
        )


class ProtocolProbeRunner:
    """Run one or more protocol cases through an explicitly injected transport."""

    def __init__(self, policy: ProtocolProbePolicy | None = None) -> None:
        self.policy = policy or ProtocolProbePolicy()

    def run(
        self,
        route_identity: RouteIdentity,
        case: ProtocolProbeCase,
        transport: ProtocolProbeTransport,
        *,
        now: datetime | None = None,
        cancel_event: Event | None = None,
        live: bool = False,
        consented: bool = False,
    ) -> ProtocolProbeObservation:
        """Run a protocol case without ever retaining the upstream payload."""

        if live and not consented:
            raise ProtocolProbeConsentRequiredError("live protocol probes require explicit consent")
        if route_identity.protocol != case.protocol:
            raise ValueError("route identity protocol does not match probe case")
        observed_at = _utc_datetime(now or datetime.now(timezone.utc))
        expires_at = observed_at + timedelta(seconds=self.policy.evidence_ttl_seconds)
        route = _safe_route_identity(route_identity)
        if cancel_event is not None and cancel_event.is_set():
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="cancelled",
                error_class="cancelled",
                limitations=("cancelled before transport scheduling",),
            )

        payload = _payload(case, route.model_id)
        started = time.monotonic()
        try:
            response = transport(route.model_id, payload, self.policy.timeout_seconds)
            if cancel_event is not None and cancel_event.is_set():
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    status="cancelled",
                    error_class="cancelled",
                    limitations=("cancelled while transport was in flight",),
                )
            if time.monotonic() - started > self.policy.timeout_seconds:
                return self._observation(
                    route, case, observed_at, expires_at, status="timeout", error_class="timeout"
                )
            if case.stream:
                return self._stream_observation(
                    route, case, response, observed_at, expires_at, cancel_event
                )
            return self._non_stream_observation(route, case, response, observed_at, expires_at)
        except Exception as exc:  # transport failures are normalized observations
            error_class = _error_class(exc)
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status=error_class,
                http_status=exc.code if isinstance(exc, urllib.error.HTTPError) else None,
                error_class=error_class,
            )

    def run_cases(
        self,
        route_identity: RouteIdentity,
        cases: Iterable[ProtocolProbeCase],
        transport: ProtocolProbeTransport,
        **kwargs: Any,
    ) -> tuple[ProtocolProbeObservation, ...]:
        """Run cases independently, preserving case order and identity."""

        return tuple(self.run(route_identity, case, transport, **kwargs) for case in cases)

    def _non_stream_observation(
        self,
        route: RouteIdentity,
        case: ProtocolProbeCase,
        response: Any,
        observed_at: datetime,
        expires_at: datetime,
    ) -> ProtocolProbeObservation:
        status_code, body, response_bytes = _response_parts(
            response, max_response_bytes=self.policy.max_response_bytes
        )
        if status_code is None:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="malformed",
                error_class="malformed_response",
                response_bytes=response_bytes,
            )
        error = _status_error_class(status_code)
        if error is not None:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status=error,
                http_status=status_code,
                error_class=error,
                response_bytes=response_bytes,
            )
        if not _valid_non_stream_body(case.protocol, body):
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="malformed",
                http_status=status_code,
                error_class="malformed_response",
                response_bytes=response_bytes,
                limitations=("HTTP success did not contain a valid protocol response",),
            )
        usage = body.get("usage")
        reasoning = _reasoning_fields_present(body)
        return self._observation(
            route,
            case,
            observed_at,
            expires_at,
            status="ready",
            http_status=status_code,
            response_bytes=response_bytes,
            usage_available=isinstance(usage, Mapping),
            reasoning_fields_present=reasoning,
        )

    def _stream_observation(
        self,
        route: RouteIdentity,
        case: ProtocolProbeCase,
        response: Any,
        observed_at: datetime,
        expires_at: datetime,
        cancel_event: Event | None,
    ) -> ProtocolProbeObservation:
        status_code, events = _stream_parts(response)
        if status_code is None:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="malformed",
                error_class="malformed_response",
            )
        error = _status_error_class(status_code)
        if error is not None:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status=error,
                http_status=status_code,
                error_class=error,
            )
        response_bytes = 0
        event_count = 0
        saw_output = False
        saw_terminal = False
        usage_available = False
        reasoning_fields_present = False
        try:
            for event in events:
                if cancel_event is not None and cancel_event.is_set():
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        status="cancelled",
                        http_status=status_code,
                        error_class="cancelled",
                        response_bytes=response_bytes,
                        event_count=event_count,
                        stream_complete=False,
                        limitations=("cancelled while consuming stream",),
                    )
                event_count += 1
                if event_count > self.policy.max_stream_events:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        status="oversized_response",
                        http_status=status_code,
                        error_class="oversized_response",
                        response_bytes=response_bytes,
                        event_count=event_count,
                        stream_complete=False,
                    )
                event_bytes = _json_size(event)
                response_bytes += event_bytes
                if response_bytes > self.policy.max_response_bytes:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        status="oversized_response",
                        http_status=status_code,
                        error_class="oversized_response",
                        response_bytes=response_bytes,
                        event_count=event_count,
                        stream_complete=False,
                    )
                if not isinstance(event, Mapping):
                    continue
                if _stream_has_output(case.protocol, event):
                    saw_output = True
                usage_available = usage_available or isinstance(event.get("usage"), Mapping)
                reasoning_fields_present = reasoning_fields_present or _reasoning_fields_present(
                    event
                )
                if _stream_is_terminal(case.protocol, event):
                    saw_terminal = True
                    break
        except (TypeError, ValueError) as exc:
            error_class = (
                "oversized_response"
                if "configured byte limit" in str(exc)
                else "malformed_response"
            )
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="malformed",
                http_status=status_code,
                error_class=error_class,
                response_bytes=response_bytes,
                event_count=event_count,
                stream_complete=False,
            )
        if not saw_terminal:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="truncated",
                http_status=status_code,
                error_class="stream_disconnected",
                response_bytes=response_bytes,
                event_count=event_count,
                stream_complete=False,
                usage_available=usage_available,
                reasoning_fields_present=reasoning_fields_present,
            )
        if not saw_output:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                status="malformed",
                http_status=status_code,
                error_class="malformed_response",
                response_bytes=response_bytes,
                event_count=event_count,
                stream_complete=True,
                usage_available=usage_available,
                reasoning_fields_present=reasoning_fields_present,
                limitations=("terminal stream contained no assistant output",),
            )
        return self._observation(
            route,
            case,
            observed_at,
            expires_at,
            status="ready",
            http_status=status_code,
            response_bytes=response_bytes,
            event_count=event_count,
            stream_complete=True,
            usage_available=usage_available,
            reasoning_fields_present=reasoning_fields_present,
        )

    @staticmethod
    def _observation(
        route: RouteIdentity,
        case: ProtocolProbeCase,
        observed_at: datetime,
        expires_at: datetime,
        *,
        status: str,
        http_status: int | None = None,
        error_class: str | None = None,
        response_bytes: int = 0,
        event_count: int = 0,
        stream_complete: bool | None = None,
        usage_available: bool = False,
        reasoning_fields_present: bool = False,
        limitations: tuple[str, ...] = (),
    ) -> ProtocolProbeObservation:
        confidence = 1.0 if status == "ready" else 0.0
        summary = {
            "version": PROTOCOL_PROBE_VERSION,
            "route": route.to_dict(),
            "case_id": case.case_id,
            "protocol": case.protocol,
            "stream": case.stream,
            "status": status,
            "http_status": http_status,
            "error_class": error_class,
            "response_bytes": response_bytes,
            "event_count": event_count,
            "stream_complete": stream_complete,
            "usage_available": usage_available,
            "reasoning_fields_present": reasoning_fields_present,
            "limitations": list(limitations),
        }
        digest = "sha256:" + hashlib.sha256(_canonical_json(summary)).hexdigest()
        return ProtocolProbeObservation(
            route_identity=route,
            case_id=case.case_id,
            protocol=case.protocol,
            stream=case.stream,
            status=status,
            observed_at=observed_at,
            expires_at=expires_at,
            confidence=confidence,
            evidence_digest=digest,
            limitations=limitations,
            http_status=http_status,
            error_class=error_class,
            response_bytes=response_bytes,
            event_count=event_count,
            stream_complete=stream_complete,
            usage_available=usage_available,
            reasoning_fields_present=reasoning_fields_present,
        )


def _payload(case: ProtocolProbeCase, model_id: str) -> dict[str, Any]:
    if case.protocol == ProtocolSurface.CHAT:
        return {
            "model": model_id,
            "messages": [{"role": "user", "content": PROTOCOL_PROBE_PROMPT}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": case.stream,
            "tools": [],
        }
    return {
        "model": model_id,
        "input": PROTOCOL_PROBE_PROMPT,
        "max_output_tokens": 1,
        "stream": case.stream,
        "tools": [],
    }


def _response_parts(
    response: Any, *, max_response_bytes: int
) -> tuple[int | None, Mapping[str, Any], int]:
    if isinstance(response, Mapping):
        status = response.get("status_code", 200)
        body = response.get("body", response)
    else:
        status = getattr(response, "status_code", None)
        try:
            body = getattr(response, "json", lambda: {})()
        except Exception as exc:
            raise ValueError("malformed response body") from exc
    if not isinstance(status, int):
        return None, {}, 0
    response_bytes = _json_size(body)
    if response_bytes > max_response_bytes:
        raise ValueError("response exceeds the configured byte limit")
    return status, body if isinstance(body, Mapping) else {}, response_bytes


def _stream_parts(response: Any) -> tuple[int | None, Iterable[Any]]:
    if isinstance(response, Mapping):
        status = response.get("status_code", 200)
        events = response.get("events", response.get("body", ()))
        if isinstance(events, Mapping):
            events = (events,)
    else:
        status = getattr(response, "status_code", 200)
        events = response
    if not isinstance(status, int) or isinstance(events, (str, bytes, bytearray)):
        return None, ()
    try:
        iterator = iter(events)
    except TypeError:
        return None, ()
    return status, iterator


def _valid_non_stream_body(protocol: str, body: Mapping[str, Any]) -> bool:
    if protocol == ProtocolSurface.CHAT:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        return any(
            isinstance(choice, Mapping)
            and isinstance(choice.get("message"), Mapping)
            and choice["message"].get("role") == "assistant"
            and _has_text(choice["message"].get("content"))
            for choice in choices
        )
    output = body.get("output")
    if not isinstance(output, list) or not output:
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("type") in {"message", "output_text"}
        and (_has_text(item.get("text")) or _has_text(item.get("content")))
        for item in output
    ) or _has_text(body.get("output_text"))


def _stream_has_output(protocol: str, event: Mapping[str, Any]) -> bool:
    if protocol == ProtocolSurface.CHAT:
        choices = event.get("choices")
        if not isinstance(choices, list):
            return False
        return any(
            isinstance(choice, Mapping)
            and isinstance(choice.get("delta"), Mapping)
            and _has_text(choice["delta"].get("content"))
            for choice in choices
        )
    return event.get("type") == "response.output_text.delta" and _has_text(event.get("delta"))


def _stream_is_terminal(protocol: str, event: Mapping[str, Any]) -> bool:
    if protocol == ProtocolSurface.CHAT:
        choices = event.get("choices")
        return (
            isinstance(choices, list)
            and any(
                isinstance(choice, Mapping) and choice.get("finish_reason") is not None
                for choice in choices
            )
        ) or event.get("type") in {"done", "[DONE]"}
    return event.get("type") in {"response.completed", "response.failed", "response.incomplete"}


def _reasoning_fields_present(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("reasoning", "reasoning_content", "reasoning_tokens"))


def _has_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(
            _has_text(item.get("text") if isinstance(item, Mapping) else item) for item in value
        )
    return False


def _status_error_class(status: int) -> str | None:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "unauthorized",
        404: "not_found",
        408: "timeout",
        409: "conflict",
        429: "rate_limited",
    }.get(status, "upstream_error" if status >= 500 else None)


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return _status_error_class(exc.code) or "http_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if "exceeds the configured byte limit" in str(exc):
        return "oversized_response"
    return "transport_error"


def _safe_route_identity(route: RouteIdentity) -> RouteIdentity:
    parsed = urllib.parse.urlsplit(route.endpoint)
    endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return replace(route, endpoint=endpoint)


def _json_size(value: Any) -> int:
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    return len(_canonical_json(value))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "CHAT_NON_STREAM_CASE",
    "CHAT_STREAM_CASE",
    "PROTOCOL_PROBE_CASES",
    "PROTOCOL_PROBE_PROMPT",
    "PROTOCOL_PROBE_VERSION",
    "RESPONSES_NON_STREAM_CASE",
    "RESPONSES_STREAM_CASE",
    "ProtocolProbeCase",
    "ProtocolProbeConsentRequiredError",
    "ProtocolProbeObservation",
    "ProtocolProbePolicy",
    "ProtocolProbeRunner",
    "ProtocolProbeTransport",
    "ProtocolSurface",
]
