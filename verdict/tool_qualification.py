"""Bounded hermetic tool lifecycle qualification for Chat and Responses routes."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, cast

from verdict.capability_passports import CapabilityEvidence, CapabilityStatus, RouteIdentity
from verdict.protocol_probes import ProtocolSurface
from verdict.structured_qualification import validate_json_instance, validate_strict_schema

TOOL_QUALIFICATION_VERSION = "1"
TOOL_PROMPT = "Use the declared tool when needed, then return a concise final answer."
TOOL_MAX_OUTPUT_TOKENS = 256
ToolHandler = Callable[[Mapping[str, Any]], Any]


class ToolQualificationConsentRequiredError(ValueError):
    """Raised before an injected transport is called without live consent."""


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
class ToolDefinition:
    """A tool declaration whose arguments are validated before execution."""

    name: str
    parameters: Mapping[str, Any]
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.name):
            raise ValueError("tool name must be a simple identifier")
        if not isinstance(self.parameters, Mapping) or self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        # A strict function declaration cannot have optional properties:
        # providers must be able to validate the complete argument object.
        validate_strict_schema(self.parameters, require_all_properties=True)


@dataclass(frozen=True)
class ToolLifecycleCase:
    """A separately reported lifecycle behavior expected from a route."""

    case_id: str
    protocol: str
    minimum_calls: int = 1
    require_parallel: bool = False
    require_error_recovery: bool = False
    max_turns: int = 4

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id is required")
        if self.protocol not in {ProtocolSurface.CHAT, ProtocolSurface.RESPONSES}:
            raise ValueError("unsupported protocol surface")
        if self.minimum_calls < 1 or self.max_turns < 1:
            raise ValueError("tool lifecycle bounds are invalid")


TOOL_VALID_ARGUMENTS_CASE = ToolLifecycleCase("tool-valid-arguments-v1", ProtocolSurface.CHAT)
TOOL_PARALLEL_CALLS_CASE = ToolLifecycleCase(
    "tool-parallel-calls-v1", ProtocolSurface.CHAT, minimum_calls=2, require_parallel=True
)
TOOL_RESULT_CONSUMPTION_CASE = ToolLifecycleCase("tool-result-consumption-v1", ProtocolSurface.CHAT)
TOOL_RESPONSES_RESULT_CONSUMPTION_CASE = ToolLifecycleCase(
    "responses-tool-result-consumption-v1", ProtocolSurface.RESPONSES
)
TOOL_ERROR_RECOVERY_CASE = ToolLifecycleCase(
    "tool-error-recovery-v1", ProtocolSurface.CHAT, require_error_recovery=True
)
TOOL_TERMINATION_CASE = ToolLifecycleCase("tool-termination-v1", ProtocolSurface.CHAT, max_turns=2)
TOOL_UNAVAILABLE_CASE = ToolLifecycleCase("tool-unavailable-rejection-v1", ProtocolSurface.CHAT)
TOOL_INJECTION_RESISTANCE_CASE = ToolLifecycleCase(
    "tool-injection-resistance-v1", ProtocolSurface.CHAT
)
# The two protocol surfaces are intentionally represented by separate cases.
# Keep the Chat constants above stable for callers that already use them and
# expose Responses counterparts for every lifecycle behavior.
TOOL_RESPONSES_VALID_ARGUMENTS_CASE = ToolLifecycleCase(
    "responses-tool-valid-arguments-v1", ProtocolSurface.RESPONSES
)
TOOL_RESPONSES_PARALLEL_CALLS_CASE = ToolLifecycleCase(
    "responses-tool-parallel-calls-v1",
    ProtocolSurface.RESPONSES,
    minimum_calls=2,
    require_parallel=True,
)
TOOL_RESPONSES_ERROR_RECOVERY_CASE = ToolLifecycleCase(
    "responses-tool-error-recovery-v1", ProtocolSurface.RESPONSES, require_error_recovery=True
)
TOOL_RESPONSES_TERMINATION_CASE = ToolLifecycleCase(
    "responses-tool-termination-v1", ProtocolSurface.RESPONSES, max_turns=2
)
TOOL_RESPONSES_UNAVAILABLE_CASE = ToolLifecycleCase(
    "responses-tool-unavailable-rejection-v1", ProtocolSurface.RESPONSES
)
TOOL_RESPONSES_INJECTION_RESISTANCE_CASE = ToolLifecycleCase(
    "responses-tool-injection-resistance-v1", ProtocolSurface.RESPONSES
)
TOOL_LIFECYCLE_CASES = (
    TOOL_VALID_ARGUMENTS_CASE,
    TOOL_PARALLEL_CALLS_CASE,
    TOOL_RESULT_CONSUMPTION_CASE,
    TOOL_RESPONSES_RESULT_CONSUMPTION_CASE,
    TOOL_ERROR_RECOVERY_CASE,
    TOOL_TERMINATION_CASE,
    TOOL_UNAVAILABLE_CASE,
    TOOL_INJECTION_RESISTANCE_CASE,
    TOOL_RESPONSES_VALID_ARGUMENTS_CASE,
    TOOL_RESPONSES_PARALLEL_CALLS_CASE,
    TOOL_RESPONSES_ERROR_RECOVERY_CASE,
    TOOL_RESPONSES_TERMINATION_CASE,
    TOOL_RESPONSES_UNAVAILABLE_CASE,
    TOOL_RESPONSES_INJECTION_RESISTANCE_CASE,
)


@dataclass(frozen=True)
class ToolLifecycleObservation:
    """Payload-free result of a bounded tool lifecycle run."""

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
    turns: int = 0
    calls_observed: int = 0
    calls_executed: int = 0
    response_bytes: int = 0
    parallel_calls_observed: bool = False
    result_round_trip: bool = False
    error_recovered: bool = False
    terminated: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.terminated and self.result_round_trip

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_qualification_version": TOOL_QUALIFICATION_VERSION,
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
            "turns": self.turns,
            "calls_observed": self.calls_observed,
            "calls_executed": self.calls_executed,
            "response_bytes": self.response_bytes,
            "parallel_calls_observed": self.parallel_calls_observed,
            "result_round_trip": self.result_round_trip,
            "error_recovered": self.error_recovered,
            "terminated": self.terminated,
        }

    def to_capability_evidence(self) -> CapabilityEvidence:
        return CapabilityEvidence(
            status=CapabilityStatus.SUPPORTED if self.ready else CapabilityStatus.UNKNOWN,
            source=f"verdict:tool-qualification/{self.case_id}",
            observed_at=self.observed_at,
            expires_at=self.expires_at,
            confidence=self.confidence,
            evidence_digest=self.evidence_digest,
            limitations=self.limitations,
        )


@dataclass(frozen=True)
class _ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]


class ToolLifecycleRunner:
    """Qualify tool behavior without granting undeclared tools execution."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        max_response_bytes: int = 1_048_576,
        max_output_tokens: int = TOOL_MAX_OUTPUT_TOKENS,
        evidence_ttl_seconds: int = 900,
    ) -> None:
        if (
            timeout_seconds <= 0
            or max_response_bytes < 1
            or max_output_tokens < 1
            or evidence_ttl_seconds < 1
        ):
            raise ValueError("tool qualification bounds are invalid")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_output_tokens = max_output_tokens
        self.evidence_ttl_seconds = evidence_ttl_seconds

    def run(
        self,
        route_identity: RouteIdentity,
        case: ToolLifecycleCase,
        transport: Any,
        tools: Mapping[str, ToolDefinition],
        handlers: Mapping[str, ToolHandler],
        *,
        now: datetime | None = None,
        cancel_event: Event | None = None,
        live: bool = False,
        consented: bool = False,
    ) -> ToolLifecycleObservation:
        if live and not consented:
            raise ToolQualificationConsentRequiredError(
                "live tool qualification requires explicit consent"
            )
        if route_identity.protocol != case.protocol:
            raise ValueError("route identity protocol does not match qualification case")
        if set(tools) != set(handlers):
            raise ValueError("tool declarations and handlers must have identical names")
        observed_at = _utc_datetime(now or datetime.now(timezone.utc))
        expires_at = observed_at + timedelta(seconds=self.evidence_ttl_seconds)
        route = _safe_route_identity(route_identity)
        payload = _initial_payload(route.model_id, case.protocol, tools, self.max_output_tokens)
        calls_observed = calls_executed = turns = 0
        response_bytes = 0
        parallel = round_trip = recovered = False
        for turn in range(1, case.max_turns + 1):
            turns = turn
            if cancel_event is not None and cancel_event.is_set():
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "cancelled",
                    "cancelled",
                    turns=turns - 1,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                    limitations=("cancelled before transport scheduling",),
                )
            try:
                response = _invoke_bounded(
                    transport,
                    (route.model_id, payload, self.timeout_seconds),
                    self.timeout_seconds,
                    cancel_event,
                )
            except TimeoutError:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "timeout",
                    "timeout",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            except _TransportCancelledError:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "cancelled",
                    "cancelled",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                    limitations=("cancelled while transport was in flight",),
                )
            except _TransportTimedOutError:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "timeout",
                    "timeout",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            except Exception:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "transport_error",
                    "transport_error",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            if cancel_event is not None and cancel_event.is_set():
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "cancelled",
                    "cancelled",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                    limitations=("cancelled while transport was in flight",),
                )
            try:
                status, body, response_size = _response_parts(response, self.max_response_bytes)
                response_bytes += response_size
                if response_bytes > self.max_response_bytes:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "oversized_response",
                        "oversized_response",
                        turns=turns,
                        calls_observed=calls_observed,
                        calls_executed=calls_executed,
                        response_bytes=response_bytes,
                        parallel_calls_observed=parallel,
                        result_round_trip=round_trip,
                        error_recovered=recovered,
                    )
            except _OversizedResponseError as exc:
                response_bytes += exc.observed_bytes
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "oversized_response",
                    "oversized_response",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            except _MalformedResponseError:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "malformed",
                    "malformed_response",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            except Exception:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "malformed",
                    "malformed_response",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            if status is None:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "malformed",
                    "malformed_response",
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            if not 200 <= status < 300:
                error_class = _status_error(status)
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    error_class,
                    error_class,
                    http_status=status,
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            parsed = _extract_calls_and_final(case.protocol, body)
            if parsed is None:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "malformed",
                    "malformed_response",
                    http_status=status,
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    response_bytes=response_bytes,
                    parallel_calls_observed=parallel,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                )
            calls, has_final = parsed
            if not calls:
                if not has_final:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "no_completion",
                        "no_completion",
                        http_status=status,
                        turns=turns,
                        calls_observed=calls_observed,
                        calls_executed=calls_executed,
                        response_bytes=response_bytes,
                        parallel_calls_observed=parallel,
                        result_round_trip=round_trip,
                        error_recovered=recovered,
                    )
                round_trip = calls_observed > 0
                if calls_observed < case.minimum_calls:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "insufficient_calls",
                        "insufficient_calls",
                        http_status=status,
                        turns=turns,
                        calls_observed=calls_observed,
                        calls_executed=calls_executed,
                        response_bytes=response_bytes,
                        result_round_trip=round_trip,
                        parallel_calls_observed=parallel,
                        error_recovered=recovered,
                        terminated=True,
                    )
                if case.require_error_recovery and not recovered:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "recovery_missing",
                        "recovery_missing",
                        http_status=status,
                        turns=turns,
                        calls_observed=calls_observed,
                        calls_executed=calls_executed,
                        response_bytes=response_bytes,
                        result_round_trip=round_trip,
                        parallel_calls_observed=parallel,
                        error_recovered=recovered,
                        terminated=True,
                    )
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "ready",
                    None,
                    http_status=status,
                    turns=turns,
                    calls_observed=calls_observed,
                    calls_executed=calls_executed,
                    parallel_calls_observed=parallel,
                    response_bytes=response_bytes,
                    result_round_trip=round_trip,
                    error_recovered=recovered,
                    terminated=True,
                )
            calls_observed += len(calls)
            parallel = parallel or len(calls) > 1
            parsed_calls: list[_ToolCall] = []
            for call in calls:
                if call.name not in tools:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "tool_unavailable",
                        "tool_unavailable",
                        http_status=status,
                        turns=turns,
                        calls_observed=calls_observed,
                        parallel_calls_observed=parallel,
                        response_bytes=response_bytes,
                        limitations=("undeclared tool call was rejected",),
                    )
                try:
                    validate_json_instance(call.arguments, tools[call.name].parameters)
                except Exception:
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "invalid_arguments",
                        "invalid_arguments",
                        http_status=status,
                        turns=turns,
                        calls_observed=calls_observed,
                        parallel_calls_observed=parallel,
                        response_bytes=response_bytes,
                    )
                parsed_calls.append(call)
            if case.require_parallel and len(parsed_calls) < 2:
                return self._observation(
                    route,
                    case,
                    observed_at,
                    expires_at,
                    "parallel_calls_missing",
                    "parallel_calls_missing",
                    http_status=status,
                    turns=turns,
                    calls_observed=calls_observed,
                    parallel_calls_observed=parallel,
                    response_bytes=response_bytes,
                )
            results: list[dict[str, Any]] = []
            for call in parsed_calls:
                try:
                    value = handlers[call.name](call.arguments)
                    results.append(
                        {"call_id": call.call_id, "name": call.name, "ok": True, "value": value}
                    )
                except Exception:
                    recovered = True
                    results.append(
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "ok": False,
                            "error": "tool execution failed",
                        }
                    )
                calls_executed += 1
                if cancel_event is not None and cancel_event.is_set():
                    return self._observation(
                        route,
                        case,
                        observed_at,
                        expires_at,
                        "cancelled",
                        "cancelled",
                        turns=turns,
                        calls_observed=calls_observed,
                        calls_executed=calls_executed,
                        response_bytes=response_bytes,
                        parallel_calls_observed=parallel,
                        result_round_trip=round_trip,
                        error_recovered=recovered,
                        limitations=("cancelled while executing tool handlers",),
                    )
            payload = _follow_up_payload(case.protocol, payload, parsed_calls, results)
        return self._observation(
            route,
            case,
            observed_at,
            expires_at,
            "loop_exhausted",
            "loop_exhausted",
            turns=turns,
            calls_observed=calls_observed,
            calls_executed=calls_executed,
            parallel_calls_observed=parallel,
            response_bytes=response_bytes,
            result_round_trip=round_trip,
            error_recovered=recovered,
        )

    @staticmethod
    def _observation(
        route: RouteIdentity,
        case: ToolLifecycleCase,
        observed_at: datetime,
        expires_at: datetime,
        status: str,
        error_class: str | None,
        **values: Any,
    ) -> ToolLifecycleObservation:
        limitations = tuple(values.pop("limitations", ()))
        summary = {
            "version": TOOL_QUALIFICATION_VERSION,
            "route": route.to_dict(),
            "case_id": case.case_id,
            "protocol": case.protocol,
            "status": status,
            "error_class": error_class,
            "limitations": list(limitations),
            **values,
        }
        digest = "sha256:" + hashlib.sha256(_canonical_json(summary)).hexdigest()
        return ToolLifecycleObservation(
            route_identity=route,
            case_id=case.case_id,
            protocol=case.protocol,
            status=status,
            observed_at=observed_at,
            expires_at=expires_at,
            confidence=1.0 if status == "ready" else 0.0,
            evidence_digest=digest,
            limitations=limitations,
            error_class=error_class,
            **values,
        )


def _initial_payload(
    model_id: str, protocol: str, tools: Mapping[str, ToolDefinition], max_output_tokens: int
) -> dict[str, Any]:
    declarations = [
        {
            "type": "function",
            "name": name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": True,
        }
        for name, tool in sorted(tools.items())
    ]
    if protocol == ProtocolSurface.CHAT:
        return {
            "model": model_id,
            "messages": [{"role": "user", "content": TOOL_PROMPT}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "description": item["description"],
                        "parameters": item["parameters"],
                        "strict": item["strict"],
                    },
                }
                for item in declarations
            ],
            "parallel_tool_calls": True,
            "max_tokens": max_output_tokens,
            "stream": False,
        }
    return {
        "model": model_id,
        "input": TOOL_PROMPT,
        "tools": [dict(item) for item in declarations],
        "parallel_tool_calls": True,
        "max_output_tokens": max_output_tokens,
        "stream": False,
    }


def _follow_up_payload(
    protocol: str, payload: dict[str, Any], calls: list[_ToolCall], results: list[dict[str, Any]]
) -> dict[str, Any]:
    next_payload = dict(payload)
    if protocol == ProtocolSurface.CHAT:
        messages = list(payload.get("messages", []))
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, sort_keys=True),
                        },
                    }
                    for call in calls
                ],
            }
        )
        messages.extend(
            {
                "role": "tool",
                "tool_call_id": result["call_id"],
                "content": json.dumps(result, sort_keys=True, default=str),
            }
            for result in results
        )
        next_payload["messages"] = messages
    else:
        items = (
            list(payload.get("input", []))
            if isinstance(payload.get("input"), list)
            else [{"role": "user", "content": TOOL_PROMPT}]
        )
        items.extend(
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, sort_keys=True),
            }
            for call in calls
        )
        items.extend(
            {
                "type": "function_call_output",
                "call_id": result["call_id"],
                "output": json.dumps(result, sort_keys=True, default=str),
            }
            for result in results
        )
        next_payload["input"] = items
    return next_payload


def _extract_calls_and_final(
    protocol: str, body: Mapping[str, Any]
) -> tuple[list[_ToolCall], bool] | None:
    calls: list[_ToolCall] = []
    final = False
    if protocol == ProtocolSurface.CHAT:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return None
        message = choices[0].get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            return None
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            return None
        for raw in raw_calls:
            if (
                not isinstance(raw, Mapping)
                or raw.get("type") != "function"
                or not isinstance(raw.get("function"), Mapping)
            ):
                return None
            function = raw["function"]
            name, arguments, call_id = (
                function.get("name"),
                function.get("arguments"),
                raw.get("id"),
            )
            if not all(isinstance(item, str) and item for item in (name, arguments, call_id)):
                return None
            try:
                parsed = json.loads(arguments, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError):
                return None
            if not isinstance(parsed, Mapping):
                return None
            calls.append(_ToolCall(cast(str, call_id), cast(str, name), parsed))
        final = (
            isinstance(message.get("content"), str)
            and bool(message["content"].strip())
            and not calls
        )
        return calls, final
    output = body.get("output")
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, Mapping):
            return None
        if item.get("type") == "function_call":
            name, arguments, call_id = item.get("name"), item.get("arguments"), item.get("call_id")
            if not all(isinstance(value, str) and value for value in (name, arguments, call_id)):
                return None
            try:
                parsed = json.loads(cast(str, arguments), parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError):
                return None
            if not isinstance(parsed, Mapping):
                return None
            calls.append(_ToolCall(cast(str, call_id), cast(str, name), parsed))
        elif item.get("type") == "message":
            if item.get("role") not in {None, "assistant"}:
                return None
            content = item.get("content")
            if not isinstance(content, list) or not any(
                isinstance(part, Mapping)
                and part.get("type") in {"output_text", "text"}
                and isinstance(part.get("text"), str)
                and bool(part["text"].strip())
                for part in content
            ):
                return None
            final = True
        elif item.get("type") == "output_text":
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                return None
            final = True
        else:
            return None
    return calls, (
        final or (isinstance(body.get("output_text"), str) and bool(body["output_text"].strip()))
    ) and not calls


def _response_parts(
    response: Any, max_response_bytes: int
) -> tuple[int | None, Mapping[str, Any], int]:
    if isinstance(response, Mapping):
        status, body = response.get("status_code", 200), response.get("body", response)
    else:
        status = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception as exc:
            raise _MalformedResponseError("response body could not be decoded") from exc
    if not isinstance(status, int) or isinstance(status, bool) or not isinstance(body, Mapping):
        raise _MalformedResponseError("response envelope is malformed")
    try:
        size = len(_canonical_json(body))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _MalformedResponseError("response body is not JSON") from exc
    if size > max_response_bytes:
        raise _OversizedResponseError("response exceeds configured byte limit", observed_bytes=size)
    return status, body, size


def _status_error(status: int) -> str:
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
    "TOOL_ERROR_RECOVERY_CASE",
    "TOOL_INJECTION_RESISTANCE_CASE",
    "TOOL_LIFECYCLE_CASES",
    "TOOL_MAX_OUTPUT_TOKENS",
    "TOOL_PARALLEL_CALLS_CASE",
    "TOOL_PROMPT",
    "TOOL_QUALIFICATION_VERSION",
    "TOOL_RESPONSES_ERROR_RECOVERY_CASE",
    "TOOL_RESPONSES_INJECTION_RESISTANCE_CASE",
    "TOOL_RESPONSES_PARALLEL_CALLS_CASE",
    "TOOL_RESPONSES_RESULT_CONSUMPTION_CASE",
    "TOOL_RESPONSES_TERMINATION_CASE",
    "TOOL_RESPONSES_UNAVAILABLE_CASE",
    "TOOL_RESPONSES_VALID_ARGUMENTS_CASE",
    "TOOL_RESULT_CONSUMPTION_CASE",
    "TOOL_TERMINATION_CASE",
    "TOOL_UNAVAILABLE_CASE",
    "TOOL_VALID_ARGUMENTS_CASE",
    "ToolDefinition",
    "ToolHandler",
    "ToolLifecycleCase",
    "ToolLifecycleObservation",
    "ToolLifecycleRunner",
    "ToolQualificationConsentRequiredError",
]
