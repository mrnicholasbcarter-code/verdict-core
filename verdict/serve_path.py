"""BOD-127 serve-path cutover: BOD-104 is sole strategy authority on serve.

Legacy selectors (IntelligenceService.route free-tier/chooser path,
``choose_route``, ``live_routing.select_route``, AdaptiveRanker,
decision_kernel, FailoverEngine, Ruflo/swarm assignment) may feed evidence or
dispatch a concrete route — they must not invent strategy outside
``optimize_execution_path``.

Migration
---------
Production API/CLI serve sets ``require_execution_path_authority`` (also via
``VERDICT_REQUIRE_EXECUTION_PATH=1`` or profile ``production``). Callers must
supply ``context["execution_path_decision"]`` (trusted in-process
``ExecutionPathDecision``) or ``context["execution_path_request"]`` for the
optimizer to run. Explicit ``allow_legacy_selector=True`` is a temporary
escape hatch for offline/unit paths only — never silent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from verdict.execution_path import (
    STRATEGY_AUTHORITY,
    ExecutionPathDecision,
    ExecutionPathError,
    ExecutionPathRequest,
    legacy_selector_must_yield,
    optimize_execution_path,
)
from verdict.session_economics import ConcreteRoute

CONTEXT_EP_DECISION = "execution_path_decision"
CONTEXT_EP_REQUEST = "execution_path_request"
CONTEXT_REQUIRE_AUTHORITY = "require_execution_path_authority"
CONTEXT_ALLOW_LEGACY = "allow_legacy_selector"

ENV_REQUIRE_EXECUTION_PATH = "VERDICT_REQUIRE_EXECUTION_PATH"

LEGACY_NON_AUTHORITY_FLAG = "legacy_non_authority_selector"
SERVE_PATH_AUTHORITY_FLAG = "bod104_execution_path_authority"


def serve_path_authority_required(
    *,
    profile: str | None = None,
    context: Mapping[str, Any] | None = None,
    require_execution_path_authority: bool | None = None,
) -> bool:
    """Whether the serve path must fail closed without a BOD-104 decision."""

    if isinstance(context, Mapping):
        if context.get(CONTEXT_ALLOW_LEGACY) is True:
            return False
        explicit = context.get(CONTEXT_REQUIRE_AUTHORITY)
        if explicit is True:
            return True
        if explicit is False:
            return False
    if require_execution_path_authority is True:
        return True
    if require_execution_path_authority is False:
        return False
    env = os.getenv(ENV_REQUIRE_EXECUTION_PATH, "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return (profile or "").strip().lower() == "production"


def resolve_execution_path_decision(
    context: Mapping[str, Any] | None,
) -> ExecutionPathDecision | None:
    """Return a trusted EP decision from context, optimizing a request if needed.

    Client-supplied dicts are rejected (same trust rule as IntelligenceService).
    """

    if not isinstance(context, Mapping):
        return None
    raw = context.get(CONTEXT_EP_DECISION)
    if raw is not None:
        if not isinstance(raw, ExecutionPathDecision):
            raise ExecutionPathError(
                "context['execution_path_decision'] must be an ExecutionPathDecision "
                "instance (client-supplied dicts are rejected)"
            )
        return raw
    request = context.get(CONTEXT_EP_REQUEST)
    if request is None:
        return None
    if not isinstance(request, ExecutionPathRequest):
        raise ExecutionPathError(
            "context['execution_path_request'] must be an ExecutionPathRequest instance"
        )
    return optimize_execution_path(request)


def require_serve_path_decision(
    decision: ExecutionPathDecision | None, *, surface: str = "production_serve"
) -> ExecutionPathDecision:
    """Fail closed when production serve lacks BOD-104 strategy authority."""

    if decision is None:
        raise ExecutionPathError(
            f"{surface}: missing ExecutionPathDecision; "
            f"strategy must come from {STRATEGY_AUTHORITY} "
            f"(set context['{CONTEXT_EP_DECISION}'] or context['{CONTEXT_EP_REQUEST}'], "
            f"or pass allow_legacy_selector only for explicit migration escapes)"
        )
    legacy_selector_must_yield(execution_path_decision=decision, legacy_selected_model_id=None)
    route = decision.selected_route
    model = None
    if isinstance(route, ConcreteRoute):
        model = route.model
    if not model:
        model = decision.selected_candidate_id
    if decision.selected_strategy == "blocked" or not model:
        raise ExecutionPathError(
            f"{surface}: BOD-104 blocked dispatch; legacy selector must not invent a route"
        )
    return decision


def selected_route_dispatch_identity(decision: ExecutionPathDecision) -> dict[str, Any]:
    """Concrete gateway/provider/model identity for dispatch-only consumers."""

    decision = require_serve_path_decision(decision, surface="dispatch")
    route = decision.selected_route
    if isinstance(route, ConcreteRoute):
        return {
            "route_id": route.route_id,
            "gateway": route.gateway,
            "provider": route.provider,
            "model": route.model,
            "capability_tier": route.capability_tier,
            "selected_strategy": decision.selected_strategy,
            "selected_candidate_id": decision.selected_candidate_id,
            "strategy_authority": STRATEGY_AUTHORITY,
        }
    return {
        "route_id": decision.selected_candidate_id,
        "gateway": "",
        "provider": "unknown",
        "model": str(decision.selected_candidate_id or ""),
        "capability_tier": 2,
        "selected_strategy": decision.selected_strategy,
        "selected_candidate_id": decision.selected_candidate_id,
        "strategy_authority": STRATEGY_AUTHORITY,
    }


def consume_selected_route(
    selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision | None,
    *,
    surface: str = "authorized_dispatch",
) -> dict[str, Any]:
    """Extract dispatch identity; fail closed if callers try to invent a route."""

    if selected_route is None:
        raise ExecutionPathError(
            f"{surface}: selected_route required; cannot independently select a model "
            f"(authority={STRATEGY_AUTHORITY})"
        )
    if isinstance(selected_route, ExecutionPathDecision):
        return selected_route_dispatch_identity(selected_route)
    if isinstance(selected_route, ConcreteRoute):
        if not selected_route.model:
            raise ExecutionPathError(f"{surface}: selected_route.model must be non-empty")
        return {
            "route_id": selected_route.route_id,
            "gateway": selected_route.gateway,
            "provider": selected_route.provider,
            "model": selected_route.model,
            "capability_tier": selected_route.capability_tier,
            "strategy_authority": STRATEGY_AUTHORITY,
        }
    if isinstance(selected_route, Mapping):
        model = selected_route.get("model") or selected_route.get("selected_candidate_id")
        if not isinstance(model, str) or not model.strip():
            raise ExecutionPathError(f"{surface}: selected_route mapping missing model")
        return {
            "route_id": str(selected_route.get("route_id") or model),
            "gateway": str(selected_route.get("gateway") or ""),
            "provider": str(selected_route.get("provider") or "unknown"),
            "model": model,
            "capability_tier": int(selected_route.get("capability_tier") or 2),
            "strategy_authority": STRATEGY_AUTHORITY,
        }
    raise ExecutionPathError(f"{surface}: unsupported selected_route type {type(selected_route)!r}")


def match_candidate_to_selected_route(
    candidates: Sequence[Any],
    selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision,
    *,
    identity_attrs: Sequence[str] = ("runtime_id", "id", "model_id", "model"),
) -> Any:
    """Pick the candidate matching BOD-104 selected_route; never invent another."""

    identity = consume_selected_route(selected_route, surface="dispatch_match")
    wanted = {
        identity["model"],
        identity["route_id"],
        str(identity.get("selected_candidate_id") or ""),
    }
    wanted.discard("")
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            values = {str(candidate.get(attr) or "") for attr in identity_attrs}
        else:
            values = {str(getattr(candidate, attr, "") or "") for attr in identity_attrs}
        if values & wanted:
            return candidate
    raise ExecutionPathError(
        f"no candidate matches selected_route={sorted(wanted)!r}; "
        "authorized dispatch must not invent an alternate"
    )


def failover_must_defer_to_bounded_recovery(
    *,
    execution_path_decision: ExecutionPathDecision | Mapping[str, Any] | None = None,
    require_bounded_recovery: bool = False,
) -> None:
    """Fail closed when FailoverEngine would independently override BOD-104/55."""

    if require_bounded_recovery or execution_path_decision is not None:
        raise ExecutionPathError(
            "FailoverEngine must not independently override BOD-104/BOD-55 recovery; "
            "use apply_bounded_recovery / BoundedRecoveryController with "
            "prequalified stronger routes"
        )


def free_tier_feed_identities(receipt: Any) -> tuple[str, ...]:
    """Expose free-tier admit as a candidate feed only (never serve authority)."""

    admitted = getattr(receipt, "admitted", ()) or ()
    return tuple(str(item) for item in admitted)


__all__ = [
    "CONTEXT_ALLOW_LEGACY",
    "CONTEXT_EP_DECISION",
    "CONTEXT_EP_REQUEST",
    "CONTEXT_REQUIRE_AUTHORITY",
    "ENV_REQUIRE_EXECUTION_PATH",
    "LEGACY_NON_AUTHORITY_FLAG",
    "SERVE_PATH_AUTHORITY_FLAG",
    "consume_selected_route",
    "failover_must_defer_to_bounded_recovery",
    "free_tier_feed_identities",
    "match_candidate_to_selected_route",
    "require_serve_path_decision",
    "resolve_execution_path_decision",
    "selected_route_dispatch_identity",
    "serve_path_authority_required",
]
