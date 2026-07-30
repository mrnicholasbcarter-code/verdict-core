"""Protocol-neutral relay planning and failover safety for OpenAI surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from verdict.capability_passports import RouteIdentity
from verdict.models import RoutingDecision
from verdict.policy import Policy, PolicyCandidate
from verdict.transitions import (
    ExecutionContext,
    RetrySafety,
    TransitionCompiler,
    TransitionEdge,
    TransitionKind,
)


@dataclass(frozen=True)
class RelayAttempt:
    """One model attempt, retaining requested and resolved identity separately."""

    model: str
    route: RouteIdentity
    candidate: PolicyCandidate


def protocol_for_surface(surface: str) -> str:
    if surface == "responses":
        return "openai.responses"
    if surface == "chat":
        return "openai.chat.completions"
    raise ValueError(f"unsupported relay surface: {surface}")


def idempotency_key(request: Any, payload: dict[str, Any]) -> str | None:
    """Read only the caller-owned header; body extensions remain transparent."""

    value = cast(str | None, request.headers.get("idempotency-key"))
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        return None
    return value


def retry_safety(request: Any, payload: dict[str, Any], key: str | None) -> RetrySafety:
    """Conservatively classify requests before permitting another POST."""

    if key is None:
        return RetrySafety.UNKNOWN
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        return RetrySafety.UNSAFE
    return RetrySafety.SAFE


def is_opaque_alias(model: str) -> bool:
    """Identify aliases whose member route cannot be inferred from the name."""

    lowered = model.strip().lower()
    return (
        lowered in {"auto", "default", "best", "router", "virtual", "combo"}
        or lowered.startswith(("auto/", "virtual/", "combo/", "router/"))
        or ":free" in lowered
    )


def retryable_transport_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def retryable_exception(error: BaseException) -> bool:
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, ConnectionError, OSError))


def failure_class(status_code: int | None = None, error: BaseException | None = None) -> str:
    if error is not None:
        if isinstance(error, httpx.TimeoutException):
            return "transport_timeout"
        if isinstance(error, (httpx.NetworkError, ConnectionError, OSError)):
            return "transport_connection"
        return "transport_error"
    if status_code in {401, 403}:
        return "auth_failure"
    if status_code in {408, 409, 425, 429}:
        return "transport_retryable"
    if status_code is not None and status_code >= 500:
        return "transport_upstream"
    if status_code in {400, 404, 422}:
        return "capability_or_request_failure"
    return "upstream_failure"


def response_actual_route_status(response: Any) -> str:
    """Normalize adapter attestation into a truthful receipt status."""

    return "verified" if getattr(response, "actual_route", None) is not None else "unavailable"


def build_attempts(
    proxy: Any, decision: RoutingDecision, *, protocol: str
) -> tuple[RelayAttempt, ...]:
    models: list[str] = [decision.model]
    admitted_alternatives = {
        str(record.get("model_id"))
        for record in decision.candidate_states
        if isinstance(record, dict)
        and record.get("admitted") is True
        and isinstance(record.get("model_id"), str)
    }
    models.extend(
        item
        for item in decision.alternatives
        if isinstance(item, str)
        and (not decision.candidate_states or item in admitted_alternatives)
    )
    attempts: list[RelayAttempt] = []
    seen: set[str] = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        route = proxy.route_identity(model, protocol)
        attempts.append(
            RelayAttempt(
                model=model,
                route=route,
                candidate=PolicyCandidate(
                    candidate_id=f"route-{route.key[7:19]}",
                    route_identity=route,
                    selected_route=route,
                    # A configured destination is the resolved route, not
                    # proof of what a gateway actually served. Actual route
                    # identity can only be supplied by the adapter attestation
                    # on the upstream response.
                    actual_route=None,
                    availability="eligible",
                    requested_alias=model if is_opaque_alias(model) else None,
                ),
            )
        )
    return tuple(attempts)


def transition_edge(
    attempts: tuple[RelayAttempt, ...],
    index: int,
    *,
    request_id: str,
    key: str | None,
    safety: RetrySafety,
    protocol: str,
    protected: bool,
) -> TransitionEdge | None:
    """Return the #116-compiled edge to the next attempt, if one exists."""

    if index + 1 >= len(attempts):
        return None
    current = attempts[index]
    target = attempts[index + 1]
    # A generic relay can attest its configured endpoint, but cannot attest
    # members hidden behind an opaque combo/virtual alias. Protected work must
    # therefore fail closed before any provider bytes are sent.
    if protected and is_opaque_alias(current.model):
        return TransitionEdge(
            current.candidate.candidate_id,
            target.candidate.candidate_id,
            kind=TransitionKind.FALLBACK,
            legal=False,
            reasons=("protected opaque alias has no actual-route attestation",),
            route_key=target.candidate.route_key,
        )
    policy = Policy(
        protected=protected,
        require_actual_identity=True,
        allowed_protocols=frozenset({protocol}),
        max_attempts=3,
    )
    graph = TransitionCompiler(policy).compile(
        current.candidate,
        [item.candidate for item in attempts[index + 1 :]],
        ExecutionContext(
            request_id=request_id,
            idempotency_key=key,
            retry_safety=safety,
            protocol=protocol,
            attempt=index,
        ),
    )
    return next(
        (edge for edge in graph.edges if edge.target == target.candidate.candidate_id), None
    )


__all__ = [
    "RelayAttempt",
    "build_attempts",
    "failure_class",
    "idempotency_key",
    "is_opaque_alias",
    "protocol_for_surface",
    "response_actual_route_status",
    "retry_safety",
    "retryable_exception",
    "retryable_transport_status",
    "transition_edge",
]
