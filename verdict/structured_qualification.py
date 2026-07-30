"""Hermetic strict structured-output qualification for exact protocol routes.

The qualification layer intentionally owns neither transport discovery nor
durable receipts.  It accepts an injected transport, validates the
protocol-specific envelope and then validates the model's JSON against a
strict schema.  Only normalized accounting is returned to callers.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, Protocol, cast

from verdict.capability_passports import CapabilityEvidence, CapabilityStatus, RouteIdentity
from verdict.protocol_probes import ProtocolSurface

STRUCTURED_QUALIFICATION_VERSION = "1"
STRUCTURED_PROMPT = "Return the requested object exactly."


class StructuredQualificationConsentRequiredError(ValueError):
    """Raised before an injected transport is called without live consent."""


class StrictSchemaError(ValueError):
    """Raised when a schema or instance is not valid under strict JSON rules."""


class StructuredQualificationTransport(Protocol):
    """Injected transport used by hermetic qualification cases."""

    def __call__(
        self, model_id: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> Any: ...


class _MalformedResponseError(ValueError):
    """Internal marker for a response that cannot be classified safely."""


class _OversizedResponseError(ValueError):
    """Internal marker for a response that exceeded the byte budget."""

    def __init__(self, message: str, observed_bytes: int) -> None:
        super().__init__(message)
        self.observed_bytes = observed_bytes


class _TransportCancelledError(Exception):
    """Internal marker for cancellation while an injected call is pending."""


class _TransportTimedOutError(Exception):
    """Internal marker for an injected call that exceeded its wall-clock bound."""


@dataclass(frozen=True)
class StructuredOutputCase:
    """One protocol-specific strict JSON response case."""

    case_id: str
    protocol: str
    schema: Mapping[str, Any]
    version: str = STRUCTURED_QUALIFICATION_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("case_id is required")
        if self.protocol not in {ProtocolSurface.CHAT, ProtocolSurface.RESPONSES}:
            raise ValueError("unsupported protocol surface")
        if self.version != STRUCTURED_QUALIFICATION_VERSION:
            raise ValueError("unsupported structured qualification version")
        validate_strict_schema(self.schema, require_all_properties=True)

    @property
    def capability(self) -> str:
        return (
            "chat.structured_output"
            if self.protocol == ProtocolSurface.CHAT
            else "responses.structured_output"
        )


@dataclass(frozen=True)
class StructuredOutputObservation:
    """Payload-free result of one strict structured-output qualification."""

    route_identity: RouteIdentity
    case_id: str
    protocol: str
    status: str
    observed_at: datetime
    expires_at: datetime
    confidence: float
    evidence_digest: str
    limitations: tuple[str, ...] = ()
    http_status: int | None = None
    error_class: str | None = None
    response_bytes: int = 0
    schema_valid: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.schema_valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "structured_qualification_version": STRUCTURED_QUALIFICATION_VERSION,
            "route_identity": self.route_identity.to_dict(),
            "case_id": self.case_id,
            "protocol": self.protocol,
            "status": self.status,
            "observed_at": _format_datetime(self.observed_at),
            "expires_at": _format_datetime(self.expires_at),
            "confidence": self.confidence,
            "evidence_digest": self.evidence_digest,
            "limitations": list(self.limitations),
            "http_status": self.http_status,
            "error_class": self.error_class,
            "response_bytes": self.response_bytes,
            "schema_valid": self.schema_valid,
        }

    def to_capability_evidence(self) -> CapabilityEvidence:
        return CapabilityEvidence(
            status=CapabilityStatus.SUPPORTED if self.ready else CapabilityStatus.UNKNOWN,
            source=f"verdict:structured-output/{self.case_id}",
            observed_at=self.observed_at,
            expires_at=self.expires_at,
            confidence=self.confidence,
            evidence_digest=self.evidence_digest,
            limitations=self.limitations,
        )


class StructuredOutputRunner:
    """Run bounded non-streaming strict-output cases through an injected route."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        max_response_bytes: int = 1_048_576,
        max_output_tokens: int = 64,
        evidence_ttl_seconds: int = 900,
    ) -> None:
        if (
            timeout_seconds <= 0
            or max_response_bytes < 1
            or max_output_tokens < 1
            or evidence_ttl_seconds < 1
        ):
            raise ValueError("structured qualification bounds are invalid")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_output_tokens = max_output_tokens
        self.evidence_ttl_seconds = evidence_ttl_seconds

    def run(
        self,
        route_identity: RouteIdentity,
        case: StructuredOutputCase,
        transport: StructuredQualificationTransport,
        *,
        now: datetime | None = None,
        cancel_event: Event | None = None,
        live: bool = False,
        consented: bool = False,
    ) -> StructuredOutputObservation:
        if live and not consented:
            raise StructuredQualificationConsentRequiredError(
                "live structured probes require explicit consent"
            )
        if route_identity.protocol != case.protocol:
            raise ValueError("route identity protocol does not match qualification case")
        observed_at = _utc_datetime(now or datetime.now(timezone.utc))
        expires_at = observed_at + timedelta(seconds=self.evidence_ttl_seconds)
        route = _safe_route_identity(route_identity)
        if cancel_event is not None and cancel_event.is_set():
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                "cancelled",
                "cancelled",
                limitations=("cancelled before transport scheduling",),
            )
        try:
            response = _invoke_bounded(
                transport,
                (
                    route.model_id,
                    _payload(route.model_id, case, self.max_output_tokens),
                    self.timeout_seconds,
                ),
                self.timeout_seconds,
                cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "cancelled",
                    "cancelled",
                    limitations=("cancelled while transport was in flight",),
                )
            status_code, body, response_bytes = _response_parts(
                response, max_response_bytes=self.max_response_bytes
            )
            if status_code is None:
                return self._observation(
                    route, case, observed_at, expires_at, "malformed", "malformed_response"
                )
            error = _status_error(status_code)
            if error:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    error,
                    error,
                    http_status=status_code,
                    response_bytes=response_bytes,
                )
            content = _extract_content(case.protocol, body)
            if content is None:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "malformed",
                    "malformed_response",
                    http_status=status_code,
                    response_bytes=response_bytes,
                    limitations=("HTTP success did not contain a protocol response",),
                )
            try:
                value = json.loads(content, parse_constant=_reject_json_constant)
                validate_json_instance(value, case.schema)
            except (json.JSONDecodeError, StrictSchemaError, ValueError):
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "schema_invalid",
                    "schema_invalid",
                    http_status=status_code,
                    response_bytes=response_bytes,
                    limitations=("response content failed strict schema validation",),
                )
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                "ready",
                None,
                http_status=status_code,
                response_bytes=response_bytes,
                schema_valid=True,
            )
        except _OversizedResponseError as exc:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                "oversized_response",
                "oversized_response",
                response_bytes=exc.observed_bytes,
            )
        except _MalformedResponseError:
            return self._observation(
                route, case, observed_at, expires_at, "malformed", "malformed_response"
            )
        except _TransportCancelledError:
            return self._observation(
                route,
                case,
                observed_at,
                expires_at,
                "cancelled",
                "cancelled",
                limitations=("cancelled while transport was in flight",),
            )
        except _TransportTimedOutError:
            return self._observation(route, case, observed_at, expires_at, "timeout", "timeout")
        except TimeoutError:
            return self._observation(route, case, observed_at, expires_at, "timeout", "timeout")
        except Exception:  # normalized; payloads and exception text never escape
            return self._observation(
                route, case, observed_at, expires_at, "transport_error", "transport_error"
            )

    @staticmethod
    def _observation(
        route: RouteIdentity,
        case: StructuredOutputCase,
        observed_at: datetime,
        expires_at: datetime,
        status: str,
        error_class: str | None,
        *,
        http_status: int | None = None,
        response_bytes: int = 0,
        schema_valid: bool = False,
        limitations: tuple[str, ...] = (),
    ) -> StructuredOutputObservation:
        summary = {
            "version": STRUCTURED_QUALIFICATION_VERSION,
            "route": route.to_dict(),
            "case_id": case.case_id,
            "protocol": case.protocol,
            "status": status,
            "error_class": error_class,
            "http_status": http_status,
            "response_bytes": response_bytes,
            "schema_valid": schema_valid,
            "limitations": list(limitations),
        }
        digest = "sha256:" + hashlib.sha256(_canonical_json(summary)).hexdigest()
        return StructuredOutputObservation(
            route_identity=route,
            case_id=case.case_id,
            protocol=case.protocol,
            status=status,
            observed_at=observed_at,
            expires_at=expires_at,
            confidence=1.0 if status == "ready" and schema_valid else 0.0,
            evidence_digest=digest,
            limitations=limitations,
            http_status=http_status,
            error_class=error_class,
            response_bytes=response_bytes,
            schema_valid=schema_valid,
        )


def validate_strict_schema(
    schema: Mapping[str, Any], *, require_all_properties: bool = False
) -> None:
    """Validate the small strict-schema vocabulary used by qualification."""
    if not isinstance(schema, Mapping) or schema.get("type") not in {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
    }:
        raise StrictSchemaError("schema must declare a supported type")
    if schema.get("type") == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise StrictSchemaError("object schema requires properties and required")
        if any(not isinstance(name, str) for name in required):
            raise StrictSchemaError("object schema required fields must be strings")
        if schema.get("additionalProperties") is not False:
            raise StrictSchemaError("object schema must set additionalProperties to false")
        names = set(properties)
        if any(not isinstance(name, str) for name in names):
            raise StrictSchemaError("object schema property names must be strings")
        if len(required) != len(set(required)):
            raise StrictSchemaError("object schema required fields must be unique")
        if set(required) - names or (require_all_properties and set(required) != names):
            raise StrictSchemaError("object schema required fields are not exact")
        for child in properties.values():
            if not isinstance(child, Mapping):
                raise StrictSchemaError("object schema properties must be schemas")
            validate_strict_schema(child, require_all_properties=require_all_properties)
    elif schema.get("type") == "array":
        if "items" not in schema or not isinstance(schema["items"], Mapping):
            raise StrictSchemaError("array schema requires items")
        validate_strict_schema(schema["items"], require_all_properties=require_all_properties)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise StrictSchemaError("enum must be a non-empty array")
    if "minLength" in schema and (
        isinstance(schema["minLength"], bool)
        or not isinstance(schema["minLength"], int)
        or schema["minLength"] < 0
    ):
        raise StrictSchemaError("minLength must be a non-negative integer")


STRICT_RESULT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "value": {"type": "string", "minLength": 1},
    },
    "required": ["status", "value"],
}

CHAT_STRICT_OUTPUT_CASE = StructuredOutputCase(
    "chat-structured-output-v1", ProtocolSurface.CHAT, STRICT_RESULT_SCHEMA
)
RESPONSES_STRICT_OUTPUT_CASE = StructuredOutputCase(
    "responses-structured-output-v1", ProtocolSurface.RESPONSES, STRICT_RESULT_SCHEMA
)
STRUCTURED_OUTPUT_CASES = (CHAT_STRICT_OUTPUT_CASE, RESPONSES_STRICT_OUTPUT_CASE)


def validate_json_instance(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate an instance with strict object/array/type/enum semantics."""
    validate_strict_schema(schema)
    expected = schema["type"]
    valid_type = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }[expected]
    if not valid_type:
        raise StrictSchemaError(f"{path} has invalid type")
    if expected == "number" and not math.isfinite(float(value)):
        raise StrictSchemaError(f"{path} has a non-finite number")
    if "enum" in schema and value not in schema["enum"]:
        raise StrictSchemaError(f"{path} is not an allowed value")
    if expected == "object":
        properties = schema["properties"]
        if set(value) - set(properties):
            raise StrictSchemaError(f"{path} has additional properties")
        for name in schema["required"]:
            if name not in value:
                raise StrictSchemaError(f"{path}.{name} is required")
        for name, child in properties.items():
            if name in value:
                validate_json_instance(value[name], child, f"{path}.{name}")
    elif expected == "array":
        for index, item in enumerate(value):
            validate_json_instance(item, schema["items"], f"{path}[{index}]")
    elif expected == "string" and len(value) < schema.get("minLength", 0):
        raise StrictSchemaError(f"{path} is too short")


def _payload(model_id: str, case: StructuredOutputCase, max_output_tokens: int) -> dict[str, Any]:
    name = case.case_id.replace("-", "_")
    if case.protocol == ProtocolSurface.CHAT:
        return {
            "model": model_id,
            "messages": [{"role": "user", "content": STRUCTURED_PROMPT}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": case.schema},
            },
            "max_tokens": max_output_tokens,
            "temperature": 0,
            "stream": False,
        }
    return {
        "model": model_id,
        "input": STRUCTURED_PROMPT,
        "text": {
            "format": {"type": "json_schema", "name": name, "strict": True, "schema": case.schema}
        },
        "max_output_tokens": max_output_tokens,
        "stream": False,
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
            body = response.json()
        except Exception as exc:
            raise _MalformedResponseError("response body could not be decoded") from exc
    if not isinstance(status, int) or isinstance(status, bool) or not isinstance(body, Mapping):
        raise _MalformedResponseError("response envelope is malformed")
    try:
        encoded = _canonical_json(body)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _MalformedResponseError("response body is not JSON") from exc
    size = len(encoded)
    if size > max_response_bytes:
        raise _OversizedResponseError("response exceeds configured byte limit", observed_bytes=size)
    return status, body, size


def _extract_content(protocol: str, body: Mapping[str, Any]) -> str | None:
    if protocol == ProtocolSurface.CHAT:
        choices = body.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                message = choice.get("message") if isinstance(choice, Mapping) else None
                content = message.get("content") if isinstance(message, Mapping) else None
                if (
                    isinstance(message, Mapping)
                    and message.get("role") == "assistant"
                    and isinstance(content, str)
                    and content.strip()
                ):
                    return content
        return None
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if item.get("type") == "message" and isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, Mapping)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                        and part["text"].strip()
                    ):
                        return cast(str, part["text"])
            if (
                item.get("type") == "output_text"
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ):
                return cast(str, item["text"])
    output_text = body.get("output_text")
    if isinstance(output_text, str):
        return output_text
    return None


def _status_error(status: int) -> str | None:
    if 200 <= status < 300:
        return None
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "unauthorized",
        404: "not_found",
        408: "timeout",
        409: "conflict",
        429: "rate_limited",
    }.get(status, "upstream_error" if status >= 500 else "unexpected_status")


def _safe_route_identity(route: RouteIdentity) -> RouteIdentity:
    parsed = urllib.parse.urlsplit(route.endpoint)
    # Strip userinfo as well as query/fragment material.  Both can contain
    # credentials even when the endpoint was supplied by an injected fixture.
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    return replace(
        route, endpoint=urllib.parse.urlunsplit((parsed.scheme, safe_netloc, parsed.path, "", ""))
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _invoke_bounded(
    callable_: Any, args: tuple[Any, ...], timeout_seconds: float, cancel_event: Event | None
) -> Any:
    """Invoke injected work without allowing a broken fixture to block the runner."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verdict-qualification")
    future = executor.submit(callable_, *args)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                future.cancel()
                raise _TransportCancelledError
            if future.done():
                return future.result()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise _TransportTimedOutError
            wait((future,), timeout=min(remaining, 0.05))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "CHAT_STRICT_OUTPUT_CASE",
    "RESPONSES_STRICT_OUTPUT_CASE",
    "STRICT_RESULT_SCHEMA",
    "STRUCTURED_OUTPUT_CASES",
    "STRUCTURED_PROMPT",
    "STRUCTURED_QUALIFICATION_VERSION",
    "StrictSchemaError",
    "StructuredOutputCase",
    "StructuredOutputObservation",
    "StructuredOutputRunner",
    "StructuredQualificationConsentRequiredError",
    "StructuredQualificationTransport",
    "validate_json_instance",
    "validate_strict_schema",
]
