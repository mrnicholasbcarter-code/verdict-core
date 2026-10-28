"""Protocol-neutral relay planning and failover safety for OpenAI surfaces."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import httpx

from verdict.capability_passports import RouteIdentity
from verdict.models import AdmissionProof, RoutingDecision
from verdict.orchestration.health_cache import (
    CATEGORY_PAYMENT,
    CATEGORY_TIMEOUT,
    CATEGORY_UPSTREAM,
    DispatchScope,
    HealthCache,
    HealthCacheError,
    HealthEntry,
    ProbeResult,
    default_cache_path,
    format_datetime,
)
from verdict.policy import Policy, PolicyCandidate
from verdict.subagent_selection import is_context_length_error

if TYPE_CHECKING:
    from verdict.admission import AdmittedSet
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


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def dispatch_scope(
    *, gateway_origin: str, provider: str, model: str, route_id: str, profile_digest: str
) -> DispatchScope:
    """Build a scope from non-secret gateway and concrete-route identity.

    This intentionally does not claim a hash of the full provider configuration.
    """
    import hashlib
    import json
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(gateway_origin)
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("dispatch origin must be a resolved gateway URL without credentials")
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    identity = json.dumps(
        {"gateway_origin": origin, "provider": provider, "model": model, "route_id": route_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    generation = "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return DispatchScope(
        gateway_origin=origin,
        config_generation=generation,
        profile_digest=profile_digest,
        binding_id=f"api_relay:{route_id}",
    )


def revocation_is_active(
    cache: HealthCache, scope: DispatchScope, route_id: str, now: datetime
) -> bool:
    """A confirm cannot lift a scoped negative before its existing backoff ends.

    Some revocations (identity/auth fences) carry no route health row. They
    use the existing authentication backoff, not a new proof lifetime.
    """
    from datetime import timedelta

    from verdict.orchestration.health_cache import CATEGORY_AUTH, negative_seconds

    revoked_at = cache.latest_revocation_at(scope)
    if revoked_at is None:
        return False
    entry = cache.entry(route_id)
    if entry is not None and entry.checked_at >= revoked_at:
        if entry.healthy and entry.checked_at > revoked_at:
            return False  # validated screen/confirm recovery, not the old proof
        if not entry.healthy:
            return entry.until > now
    return revoked_at + timedelta(seconds=negative_seconds(CATEGORY_AUTH)) > now


def admission_proof(
    confirmation: dict[str, Any] | None,
    *,
    route_id: str,
    scope: DispatchScope,
    authorization_digest: str,
    provider: str,
    model: str,
    now: datetime,
) -> AdmissionProof | None:
    """Extract only the selected route's fresh passport from trusted confirmation."""
    if not isinstance(confirmation, dict):
        return None
    if route_id not in confirmation.get("admitted", []):
        return None
    receipt_id = confirmation.get("receipt_id")
    passports = confirmation.get("passport")
    if not isinstance(receipt_id, str) or not receipt_id or not isinstance(passports, list):
        return None
    for row in passports:
        if (
            not isinstance(row, dict)
            or row.get("identity_id") != route_id
            or row.get("fresh") is not True
            or row.get("auth_state") != "authorized"
            or row.get("availability_state") != "eligible"
        ):
            continue
        qualified_at = _parse_utc(row.get("qualified_at"))
        expires_at = _parse_utc(row.get("expires_at"))
        if qualified_at is None or expires_at is None or qualified_at > now or expires_at <= now:
            return None
        cache = HealthCache(default_cache_path())
        latest_revocation = cache.latest_revocation_at(scope)
        negative = cache.entry(route_id)
        if negative is not None and not negative.healthy and negative.until > now:
            return None
        if revocation_is_active(cache, scope, route_id, now):
            return None
        if latest_revocation is not None and qualified_at <= latest_revocation:
            return None  # passport predates the latest scoped revocation
        epoch = cache.revocation_epoch(scope)
        return AdmissionProof(
            route_id=route_id,
            provider=provider,
            model=model,
            qualified_at=qualified_at,
            expires_at=expires_at,
            receipt_id=receipt_id,
            authorization_digest=authorization_digest,
            epoch=epoch,
            scope=scope,
        )
    return None


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


def fatal_identity_mismatch(selected: str, served: str | None) -> bool:
    """True when a concrete selected identity was silently substituted.

    Opaque aliases may resolve to a different served member; that remap is
    observed, not fatal. A concrete selected model that comes back as a
    different model must fail closed before output is trusted.
    """

    if not selected or served is None or served == selected:
        return False
    # A gateway may report a provider-local model id for a namespaced concrete
    # route. This preserves the selected identity rather than substituting it.
    if "/" in selected and served == selected.split("/", 1)[1]:
        return False
    return not is_opaque_alias(selected)


def retryable_transport_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def retryable_response_status(
    status_code: int, *, compatibility_applied: bool, body: str = ""
) -> bool:
    """Allow ordinary transient retries but stop deterministic compatibility 4xx errors.

    Context-length overflow (400/413 with a recognised marker in *body*) is
    retryable to a **different** model only.  ``build_attempts`` already
    de-duplicates models, so allowing the retry here is safe — the next
    attempt will always be a distinct model.
    """
    if status_code in {400, 413} and is_context_length_error(body):
        return True
    if status_code in {400, 404, 422} and any(
        marker in body.lower()
        for marker in (
            "tool_not_supported",
            "tools_not_supported",
            "unsupported_tool",
            "does not support tools",
        )
    ):
        return True
    if compatibility_applied and status_code in {400, 404, 422}:
        return False
    return retryable_transport_status(status_code)


def failure_class(
    status_code: int | None = None, error: BaseException | None = None, *, body: str = ""
) -> str:
    if error is not None:
        if isinstance(error, httpx.TimeoutException):
            return "transport_timeout"
        if isinstance(error, (httpx.NetworkError, ConnectionError, OSError)):
            return "transport_connection"
        # Context-length errors may surface as exceptions with status embedded.
        error_text = str(error)
        if is_context_length_error(error_text):
            return "context_length_overflow"
        return "transport_error"
    # Context-length overflow is request-scoped; not a provider/model defect.
    if status_code in {400, 413} and is_context_length_error(body):
        return "context_length_overflow"
    if status_code == 402:
        return "payment_required"
    if status_code in {401, 403}:
        return "auth_failure"
    if status_code in {408, 409, 425, 429}:
        return "transport_retryable"
    if status_code is not None and status_code >= 500:
        return "transport_upstream"
    if status_code in {400, 404, 422}:
        return "capability_or_request_failure"
    return "upstream_failure"


# Real relay failures that are availability evidence for the shared cache.
# Everything else (caller, input, auth, capability and generic request errors)
# stays ledger-only; auth and model-404 keep the dispatch revocation path.
_RELAY_FEEDBACK_CATEGORIES: Mapping[str, str] = {
    "payment_required": CATEGORY_PAYMENT,
    "transport_upstream": CATEGORY_UPSTREAM,
    "transport_timeout": CATEGORY_TIMEOUT,
    "transport_connection": CATEGORY_UPSTREAM,
}


@dataclass(frozen=True)
class RelayFeedbackResult:
    """Write acknowledgement for one relay failure. Skips carry an explicit reason."""

    recorded: bool
    reason: str
    entry: HealthEntry | None = None


def relay_failure_feedback(
    cache: HealthCache,
    *,
    route_id: str,
    status_code: int | None = None,
    error: BaseException | None = None,
    body: str = "",
    now: datetime,
    provenance: str | None = None,
) -> RelayFeedbackResult:
    """Project one real relay dispatch failure into the shared health cache.

    Classification reuses ``failure_class``; only upstream 5xx responses and
    transport timeout/connection failures are availability evidence. The
    negative is route-scoped through the same serialized writer
    (``merge_and_save`` -> ``record``) orchestrate session feedback uses, with
    the same skip/report semantics: non-availability failures return an
    explicit reason instead of writing, a newer cache observation wins, and
    lock/persistence errors propagate so the caller can report the failed
    write. One failing model or request never blocks sibling routes, pools or
    the provider.
    """
    failure = failure_class(status_code, error, body=body)
    category = _RELAY_FEEDBACK_CATEGORIES.get(failure)
    if category is None:
        return RelayFeedbackResult(False, "non_availability_failure")
    if not isinstance(route_id, str) or not route_id.strip():
        raise HealthCacheError("selected route must be non-empty")
    format_datetime(now)  # Reject naive timestamps before mutating any state.
    opaque_source = None
    if provenance:
        digest = hashlib.sha256(provenance.encode("utf-8")).hexdigest()
        opaque_source = f"sha256:{digest}"
    result = RelayFeedbackResult(False, "not_written")

    def _mutate(locked: HealthCache) -> None:
        nonlocal result
        previous = locked.entry(route_id)
        if previous is not None and (
            previous.checked_at > now or (previous.checked_at == now and not previous.healthy)
        ):
            result = RelayFeedbackResult(False, "superseded_or_duplicate")
            return
        entry = locked.record(
            route_id,
            ProbeResult(
                category=category,
                chat_ok=False,
                tool_ok=False,
                http_status=status_code,
                failure_scope="route",
                feedback_source="serve_request",
                feedback_category=failure,
                capacity_evidence="billing_402_metered" if category == CATEGORY_PAYMENT else None,
                feedback_provenance=opaque_source,
            ),
            now,
        )
        result = RelayFeedbackResult(True, "recorded", entry)

    cache.merge_and_save(_mutate)
    # Return the revision allocated by the serialized writer, not its pre-write copy.
    if result.recorded:
        result = RelayFeedbackResult(True, "recorded", cache.entry(route_id))
    return result


def response_actual_route_status(response: Any) -> str:
    """Normalize adapter attestation into a truthful receipt status."""

    return "verified" if getattr(response, "actual_route", None) is not None else "unavailable"


def build_attempts(
    proxy: Any, decision: RoutingDecision, *, protocol: str, admitted: AdmittedSet | None = None
) -> tuple[RelayAttempt, ...]:
    """Build distinct relay candidates from the selected model and admitted alternatives.

    When subscription fallback policy applies, reserve subscription and metered
    candidates after free routes within the existing three-attempt budget.
    """
    models: list[str] = [decision.model]
    admitted_alternatives = {
        str(record.get("model_id"))
        for record in decision.candidate_states
        if isinstance(record, dict)
        and record.get("admitted") is True
        and isinstance(record.get("model_id"), str)
    }
    # Alternatives may only narrow the admitted set. Without per-candidate
    # admission evidence an alternative is unproven, so it is dropped rather
    # than silently treated as eligible.
    models.extend(
        item
        for item in decision.alternatives
        if isinstance(item, str)
        and item in admitted_alternatives
        and (admitted is None or item in admitted)
    )
    if admitted is not None and decision.model not in admitted:
        return ()
    if "subscription_fallback_policy" in decision.safety_flags:
        capacities = {
            row.get("model_id"): row.get("capacity_order") for row in decision.candidate_states
        }
        models = [model for model in models if capacities.get(model) in (0, 1, 2)]
        free = [model for model in models if capacities.get(model) == 0]
        subscription = [model for model in models if capacities.get(model) == 1]
        metered = [model for model in models if capacities.get(model) == 2]
        # Fallback tiers consume the same three-attempt budget, never extend it.
        reserved = [*subscription[:1], *metered[:1]]
        models = (
            [*free[: 3 - len(reserved)], *reserved]
            if free
            else [*subscription[: 2 if metered else 3], *metered[:1]]
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
    "fatal_identity_mismatch",
    "idempotency_key",
    "is_opaque_alias",
    "protocol_for_surface",
    "response_actual_route_status",
    "retry_safety",
    "retryable_response_status",
    "retryable_transport_status",
    "transition_edge",
]
