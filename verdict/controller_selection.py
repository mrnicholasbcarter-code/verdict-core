"""BOD-156: live controller selection before Prime launch.

``select_controller_launch`` generates an exact Verdict decision from live
eligibility (BOD-142), candidate ContextPlans (BOD-143), BOD-104 ranking, and
optional BOD-119 session continuity. It persists a BOD-144 RoutingReceiptV1
before returning a launchable ``ControllerLaunchDecision``.

This module does not reimplement ranking. Authorities are called through
injected callables / ``IntelligenceService.prepare_controller_execution_request``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from verdict.controller_launch import (
    ControllerLaunchDecision,
    ControllerLaunchError,
    ControllerMission,
    OperatorOverride,
    PersistedAuthoritativeDecision,
    PrimeLaunchTarget,
    decide_controller_launch,
)
from verdict.cost_ledger import PriceEvidenceInput
from verdict.execution_path import (
    ExecutionPathDecision,
    ExecutionPathError,
    ExecutionPathOffer,
    ExecutionPathRequest,
    optimize_execution_path,
)
from verdict.routing_receipt import (
    build_routing_receipt,
    default_receipt_store,
    persist_routing_receipt,
)
from verdict.runtime_certification import (
    CertificationState,
    ComponentKind,
    DetectedSnapshot,
    RuntimeCertificationReport,
    certify_runtime,
)
from verdict.session_economics import (
    ConcreteRoute,
    CostState,
    SessionRouteDecision,
    SessionState,
    TaskState,
    decide_session_route,
)

PrepareControllerFn = Callable[
    [str, str, dict[str, Any], ExecutionPathRequest], ExecutionPathRequest
]
OptimizeFn = Callable[[ExecutionPathRequest], ExecutionPathDecision]
SessionDecideFn = Callable[..., SessionRouteDecision]
BuildReceiptFn = Callable[..., Any]
PersistReceiptFn = Callable[..., Any]
BindTargetFn = Callable[[ConcreteRoute], PrimeLaunchTarget]


class RuntimeCertifyFn(Protocol):
    """Offline BOD-92 certification seam; never grants selection authority."""

    def __call__(
        self,
        *,
        snapshots: Sequence[DetectedSnapshot],
        now: datetime,
        run_registered_detectors: bool,
    ) -> RuntimeCertificationReport: ...


class ControllerSelector(Protocol):
    """Injectable selection surface used by the supervisor and unit tests."""

    def select_controller_launch(
        self,
        mission: ControllerMission,
        *,
        override: OperatorOverride | None = None,
        session_state: SessionState | None = None,
        now: datetime | None = None,
    ) -> ControllerLaunchDecision: ...


@dataclass(frozen=True)
class ControllerSelectionHooks:
    """Callables / fixtures that keep unit tests offline and production wired."""

    prepare_execution_request: PrepareControllerFn
    seed_offers: Callable[[ControllerMission, datetime], Sequence[ExecutionPathOffer]]
    optimize: OptimizeFn = optimize_execution_path
    decide_session: SessionDecideFn = decide_session_route
    build_receipt: BuildReceiptFn = build_routing_receipt
    persist_receipt: PersistReceiptFn = persist_routing_receipt
    bind_prime_target: BindTargetFn | None = None
    receipt_store: Any | None = None
    workspace_root: Path | str | None = None
    criticality: str = "high"
    decision_ttl: timedelta = timedelta(hours=1)
    cost_state_factory: Callable[[ConcreteRoute, ConcreteRoute, datetime], CostState] | None = None
    task_state_factory: Callable[[ControllerMission], TaskState] | None = None
    compile_context_digests: (
        Callable[[ExecutionPathDecision, ExecutionPathRequest], Mapping[str, Any]] | None
    ) = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_auto_identity(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ControllerLaunchError("invalid_field", f"{field_name} must be non-empty")
    lowered = text.lower()
    if lowered.startswith("auto/") or lowered in {"auto", "default", "*"} or "/auto/" in lowered:
        raise ControllerLaunchError(
            "forbidden_identity", f"{field_name} rejects auto/default/opaque identity: {text!r}"
        )
    return text


def _reject_identity_shaped_model(
    value: str, field_name: str, *, gateway: str | None = None
) -> str:
    """Reject full gateway identity ids used where a leaf/model segment is required.

    Example forbidden as ``prime_model`` / ``ConcreteRoute.model``:
    ``omniroute/gc/grok-4.5`` when gateway is ``omniroute``.
    """
    text = _reject_auto_identity(value, field_name)
    gw = (gateway or "").strip().lower()
    if gw and text.lower().startswith(f"{gw}/"):
        raise ControllerLaunchError(
            "identity_shaped_model",
            f"{field_name} must be leaf model, not full identity id: {text!r}",
        )
    # Three-or-more-segment ids are identity-shaped even without an explicit gateway.
    if text.count("/") >= 2:
        raise ControllerLaunchError(
            "identity_shaped_model",
            f"{field_name} must be leaf model, not full identity id: {text!r}",
        )
    return text


def _normalize_passport_identity(
    identity_id: str, passport: Any, *, gateway: str
) -> tuple[str, str, str]:
    """Return ``(provider, leaf_model, route_id)`` from passport + inventory id.

    ``passport.model_id`` may already be a full identity (``omniroute/gc/grok-4.5``).
    ``ConcreteRoute.model`` must be the leaf/model segment (``gc/grok-4.5``);
    ``route_id`` may keep the full inventory identity.
    """
    raw_id = (identity_id or "").strip()
    if not raw_id:
        raise ControllerLaunchError("invalid_field", "passport identity_id must be non-empty")
    provider = str(getattr(passport, "provider", "") or "").strip()
    raw_model = str(getattr(passport, "model_id", "") or "").strip() or raw_id
    gw = (gateway or "omniroute").strip() or "omniroute"

    route_id = raw_id if "/" in raw_id else f"{provider or gw}/{raw_model}"

    # Prefer stripping known gateway prefix from full identity / model_id.
    leaf_source = raw_model
    for candidate in (raw_model, raw_id, route_id):
        text = candidate.strip()
        if text.lower().startswith(f"{gw.lower()}/"):
            leaf_source = text[len(gw) + 1 :]
            break
    else:
        # identity_id shaped as provider/model[/...] without gateway.
        if raw_id.count("/") >= 1 and raw_model == raw_id:
            leaf_source = raw_id.split("/", 1)[1] if raw_id.split("/", 1)[0] == provider else raw_id

    if not provider:
        if raw_id.lower().startswith(f"{gw.lower()}/") and "/" in raw_id[len(gw) + 1 :]:
            # omniroute/gc/grok-4.5 → provider from first leaf segment only when
            # passport omitted it; keep gateway as ConcreteRoute.provider for Prime.
            provider = gw
        elif "/" in raw_id:
            provider = raw_id.split("/", 1)[0]
        else:
            provider = gw

    # When passport.provider is the gateway and model_id was full identity,
    # leaf_source is already gateway-stripped (gc/grok-4.5). Keep provider=gateway.
    if provider.lower() == gw.lower() and leaf_source.lower().startswith(f"{gw.lower()}/"):
        leaf_source = leaf_source[len(gw) + 1 :]

    leaf_model = _reject_identity_shaped_model(leaf_source, "offer.model", gateway=gw)
    provider = _reject_auto_identity(provider, "offer.provider")
    route_id = _reject_auto_identity(route_id, "offer.route_id")
    return provider, leaf_model, route_id


def _default_bind_prime_target(route: ConcreteRoute) -> PrimeLaunchTarget:
    """Identity-preserving Prime binding. Upstream and Prime remain exact."""
    provider = _reject_auto_identity(route.provider, "prime_provider")
    model = _reject_identity_shaped_model(route.model, "prime_model", gateway=route.gateway)
    upstream_provider = _reject_auto_identity(route.provider, "upstream_provider")
    upstream_model = _reject_identity_shaped_model(
        route.model, "upstream_model", gateway=route.gateway
    )
    binding = f"prime-bind:{route.route_id}:{provider}:{model}"
    return PrimeLaunchTarget(
        upstream_provider=upstream_provider,
        upstream_model=upstream_model,
        prime_provider=provider,
        prime_model=model,
        binding_digest=binding,
        reasoning_effort=None,
        binding_evidence_ref=f"route://{route.route_id}",
    )


def _route_from_offer(offer: ExecutionPathOffer) -> ConcreteRoute:
    return offer.route


def _selected_context_artifacts(
    decision: ExecutionPathDecision,
    prepared: ExecutionPathRequest,
    *,
    compiler: Callable[[ExecutionPathDecision, ExecutionPathRequest], Mapping[str, Any]] | None,
) -> tuple[Mapping[str, str], Any, Any, Any]:
    """Compile digests and require real context objects for the receipt (M4)."""
    if compiler is None:
        raise ControllerLaunchError(
            "missing_context_compiler",
            "controller selection requires compile_context_digests; "
            "placeholder pack/receipt/prompt digests are forbidden",
        )
    compiled = dict(compiler(decision, prepared))
    digests: dict[str, str] = {}
    for key in (
        "context_plan_digest",
        "context_pack_digest",
        "context_receipt_digest",
        "selected_prompt_digest",
    ):
        if key not in compiled or not str(compiled[key]).strip():
            raise ControllerLaunchError(
                "missing_context_digest", f"context compiler omitted required {key}"
            )
        digests[key] = str(compiled[key])
    context_plan = compiled.get("context_plan")
    context_pack = compiled.get("context_pack")
    context_receipt = compiled.get("context_receipt")
    if context_plan is None or context_pack is None or context_receipt is None:
        raise ControllerLaunchError(
            "missing_context_objects",
            "context compiler must return context_plan, context_pack, and "
            "context_receipt objects for RoutingReceiptV1; digests alone are not authority",
        )
    return digests, context_plan, context_pack, context_receipt


def _mark_prepared_offers_eligible(
    offers: Sequence[ExecutionPathOffer],
) -> tuple[ExecutionPathOffer, ...]:
    """Promote prepare-confirmed seed offers to eligible for BOD-104 (M1)."""
    promoted: list[ExecutionPathOffer] = []
    for offer in offers:
        route = offer.route
        if route.eligible and not route.excluded:
            promoted.append(offer)
            continue
        promoted.append(
            replace(
                offer,
                route=ConcreteRoute(
                    route_id=route.route_id,
                    gateway=route.gateway,
                    provider=route.provider,
                    model=route.model,
                    credential_pool=route.credential_pool,
                    capability_tier=route.capability_tier,
                    eligible=True,
                    excluded=False,
                    exclusion_reason=None,
                ),
            )
        )
    return tuple(promoted)


def _reconcile_session_label_with_ep(
    session_decision: SessionRouteDecision | None, *, selected_route: ConcreteRoute, fresh: bool
) -> tuple[str, str | None]:
    """Ensure receipt STAY/SWITCH matches ep_decision.selected_route (M3).

    Returns ``(label, rewrite_reason_or_none)``. Fails closed when a STAY
    claim would launch a different model/route.
    """
    if session_decision is None:
        return ("NEW" if fresh else "UNKNOWN", None)
    label = str(session_decision.decision)
    selected_id = selected_route.route_id
    session_selected = session_decision.selected_route
    session_selected_id = session_decision.selected_route_id
    if session_selected is not None:
        session_selected_id = session_selected.route_id
    if label == "STAY" and session_selected_id and session_selected_id != selected_id:
        raise ControllerLaunchError(
            "session_ep_mismatch",
            "session decision claimed STAY on "
            f"{session_selected_id!r} but BOD-104 selected {selected_id!r}; "
            "refusing to receipt-claim STAY while launching another model",
        )
    if label == "SWITCH" and session_selected_id and session_selected_id != selected_id:
        # EP remains authoritative; rewrite label to match launched route.
        return (
            "SWITCH",
            f"session_switch_target_rewritten_to_ep:{session_selected_id}->{selected_id}",
        )
    if (
        label == "STAY"
        and session_selected is not None
        and (
            session_selected.provider != selected_route.provider
            or session_selected.model != selected_route.model
        )
    ):
        raise ControllerLaunchError(
            "session_ep_mismatch",
            "session STAY provider/model diverges from BOD-104 selected route",
        )
    return (label, None)


def _mission_task_slice(mission: ControllerMission) -> Any:
    from verdict.effective_capability import TaskSlice

    # Stable controller proof/acceptance criteria. Offer builders and the
    # selector share this contract so BOD-104 evidence binding stays exact.
    proof = (mission.proof_burden or "controller-launch-proof",)
    acceptance = (mission.objective,)
    return TaskSlice(
        slice_id=mission.attempt_id,
        objective=mission.objective,
        acceptance_criteria=acceptance,
        proof_criteria=proof,
    )


def _find_offer(offers: Sequence[ExecutionPathOffer], route_id: str) -> ExecutionPathOffer | None:
    for offer in offers:
        if offer.route.route_id == route_id:
            return offer
    return None


def _offer_matches_override(offer: ExecutionPathOffer, override: OperatorOverride) -> bool:
    route = offer.route
    return (route.provider == override.provider and route.model == override.model) or (
        route.route_id == override.model
        or route.route_id.endswith(f"/{override.model}")
        or route.route_id == f"{override.provider}/{override.model}"
    )


def _constrain_offers_to_override(
    offers: Sequence[ExecutionPathOffer], override: OperatorOverride
) -> tuple[ExecutionPathOffer, ...]:
    matched = tuple(offer for offer in offers if _offer_matches_override(offer, override))
    if not matched:
        raise ControllerLaunchError(
            "override_not_eligible",
            "explicit override provider/model is not in the live eligible set",
        )
    return matched


def _session_decision_label(decision: SessionRouteDecision | None, *, fresh: bool) -> str:
    if decision is None:
        return "NEW" if fresh else "UNKNOWN"
    return str(decision.decision)


def select_controller_launch(
    mission: ControllerMission,
    *,
    hooks: ControllerSelectionHooks,
    override: OperatorOverride | None = None,
    session_state: SessionState | None = None,
    now: datetime | None = None,
) -> ControllerLaunchDecision:
    """Generate and persist an exact controller launch decision.

    Pipeline:
      seed live offers -> prepare (eligibility + ContextPlan) -> optional
      BOD-119 session decide -> BOD-104 optimize -> bind Prime target ->
      persist RoutingReceiptV1 -> assemble ControllerLaunchDecision.

    Returns a validated decision, or raises ``ControllerLaunchError`` with
    ``no_eligible_route`` / named evidence failures. Never launches ``auto/*``.
    """
    when = now or _utc_now()
    if override is not None and override.source != "cli":
        raise ControllerLaunchError(
            "untrusted_override_source", "OperatorOverride.source must be 'cli'"
        )

    try:
        seed_offers = tuple(hooks.seed_offers(mission, when))
    except ControllerLaunchError:
        raise
    except Exception as exc:
        raise ControllerLaunchError(
            "seed_offers_failed", f"failed to build live seed offers: {exc}"
        ) from exc

    if not seed_offers:
        raise ControllerLaunchError(
            "no_eligible_route", "no live seed offers available for controller selection"
        )

    for offer in seed_offers:
        _reject_auto_identity(offer.route.provider, "offer.provider")
        _reject_auto_identity(offer.route.model, "offer.model")
        _reject_auto_identity(offer.route.route_id, "offer.route_id")

    task_slice = _mission_task_slice(mission)
    trajectory_id = mission.trajectory_digest or f"controller:{mission.attempt_id}"
    # Rebind offer evidence to this mission's TaskSlice/trajectory so BOD-104
    # qualification does not reject otherwise-live offers as unbound.
    rebound: list[ExecutionPathOffer] = []
    for offer in seed_offers:
        assistance = replace(offer.assistance_plan, task_slice=task_slice)
        expected = replace(offer.expected_cost, trajectory_id=trajectory_id)
        rebound.append(replace(offer, assistance_plan=assistance, expected_cost=expected))
    seed_offers = tuple(rebound)
    seed_request = ExecutionPathRequest(
        task_slice=task_slice,
        trajectory_id=trajectory_id,
        offers=seed_offers,
        now=when,
        assumptions=("controller_selection_seed",),
    )
    context: dict[str, Any] = {
        "controller_mission_id": mission.mission_id,
        "story_id": mission.story_id,
        "attempt_id": mission.attempt_id,
        "required_tools": list(mission.required_tools),
        "required_mcp": list(mission.required_mcp),
        "orchestration_burden": mission.orchestration_burden,
        "context_burden": mission.context_burden,
        "proof_burden": mission.proof_burden,
        "acceptance_criteria": [mission.objective],
        "proof_criteria": [mission.proof_burden or "controller-launch-proof"],
    }

    try:
        prepared = hooks.prepare_execution_request(
            mission.objective, hooks.criticality, context, seed_request
        )
    except ExecutionPathError as exc:
        detail = str(exc)
        lowered = detail.lower()
        if "snapshot" in lowered or "missing live" in lowered:
            raise ControllerLaunchError("missing_snapshot", detail) from exc
        if "metadata" in lowered:
            raise ControllerLaunchError("missing_metadata", detail) from exc
        if "stale" in lowered:
            raise ControllerLaunchError("stale_evidence", detail) from exc
        raise ControllerLaunchError("eligibility_preparation_failed", detail) from exc
    except ControllerLaunchError:
        raise
    except Exception as exc:
        raise ControllerLaunchError(
            "eligibility_preparation_failed", f"live eligibility preparation failed: {exc}"
        ) from exc

    # prepare_controller_execution_request is the eligibility authority (M1).
    # Promote surviving inventory seeds to eligible only after prepare confirms.
    eligible_offers = _mark_prepared_offers_eligible(prepared.offers)
    prepared = ExecutionPathRequest(
        task_slice=prepared.task_slice,
        trajectory_id=prepared.trajectory_id,
        offers=eligible_offers,
        session_decision=prepared.session_decision,
        recovery_bounds=prepared.recovery_bounds,
        pool_receipt=prepared.pool_receipt,
        hard_excluded_ids=prepared.hard_excluded_ids,
        now=prepared.now,
        assumptions=tuple([*prepared.assumptions, "prepare_confirmed_eligible"]),
        allow_degraded_certification=prepared.allow_degraded_certification,
        require_complete_cost_kinds=prepared.require_complete_cost_kinds,
        prequalified_stronger_route_ids=prepared.prequalified_stronger_route_ids,
        evidence_conflicts=prepared.evidence_conflicts,
    )
    if override is not None:
        eligible_offers = _constrain_offers_to_override(prepared.offers, override)
        prepared = ExecutionPathRequest(
            task_slice=prepared.task_slice,
            trajectory_id=prepared.trajectory_id,
            offers=eligible_offers,
            session_decision=prepared.session_decision,
            recovery_bounds=prepared.recovery_bounds,
            pool_receipt=prepared.pool_receipt,
            hard_excluded_ids=prepared.hard_excluded_ids,
            now=prepared.now,
            assumptions=tuple([*prepared.assumptions, "operator_override_constrained"]),
            allow_degraded_certification=prepared.allow_degraded_certification,
            require_complete_cost_kinds=prepared.require_complete_cost_kinds,
            prequalified_stronger_route_ids=prepared.prequalified_stronger_route_ids,
            evidence_conflicts=prepared.evidence_conflicts,
        )

    if not prepared.offers:
        raise ControllerLaunchError(
            "no_eligible_route",
            "live eligibility + context plans left no qualifying controller route",
        )

    session_decision: SessionRouteDecision | None = None
    if session_state is not None:
        # Rank fresh qualified route first among prepared offers for SWITCH compare.
        # We still run BOD-104 after attaching the session decision.
        provisional = hooks.optimize(
            ExecutionPathRequest(
                task_slice=prepared.task_slice,
                trajectory_id=prepared.trajectory_id,
                offers=prepared.offers,
                pool_receipt=prepared.pool_receipt,
                hard_excluded_ids=prepared.hard_excluded_ids,
                now=when,
                assumptions=tuple([*prepared.assumptions, "session_provisional_rank"]),
                allow_degraded_certification=prepared.allow_degraded_certification,
                require_complete_cost_kinds=prepared.require_complete_cost_kinds,
                prequalified_stronger_route_ids=prepared.prequalified_stronger_route_ids,
                evidence_conflicts=prepared.evidence_conflicts,
            )
        )
        if provisional.selected_route is None or provisional.selected_strategy == "blocked":
            raise ControllerLaunchError(
                "no_eligible_route",
                "provisional BOD-104 ranking found no eligible controller route",
            )
        fresh_route = provisional.selected_route
        if hooks.cost_state_factory is None or hooks.task_state_factory is None:
            raise ControllerLaunchError(
                "missing_session_hooks",
                "session continuity requires cost_state_factory and task_state_factory",
            )
        cost_state = hooks.cost_state_factory(session_state.current_route, fresh_route, when)
        task_state = hooks.task_state_factory(mission)
        session_decision = hooks.decide_session(session_state, fresh_route, cost_state, task_state)
        if session_decision.decision == "BLOCKED":
            raise ControllerLaunchError(
                "no_eligible_route",
                f"session economics blocked controller launch: {session_decision.reason}",
            )
        prepared = ExecutionPathRequest(
            task_slice=prepared.task_slice,
            trajectory_id=prepared.trajectory_id,
            offers=prepared.offers,
            session_decision=session_decision,
            recovery_bounds=prepared.recovery_bounds,
            pool_receipt=prepared.pool_receipt,
            hard_excluded_ids=prepared.hard_excluded_ids,
            now=when,
            assumptions=tuple(
                [*prepared.assumptions, f"session_decision={session_decision.decision}"]
            ),
            allow_degraded_certification=prepared.allow_degraded_certification,
            require_complete_cost_kinds=prepared.require_complete_cost_kinds,
            prequalified_stronger_route_ids=prepared.prequalified_stronger_route_ids,
            evidence_conflicts=prepared.evidence_conflicts,
        )

    try:
        ep_decision = hooks.optimize(prepared)
    except ExecutionPathError as exc:
        raise ControllerLaunchError("execution_path_failed", str(exc)) from exc

    if ep_decision.selected_route is None or ep_decision.selected_strategy == "blocked":
        raise ControllerLaunchError(
            "no_eligible_route",
            ep_decision.why_selected or "BOD-104 returned blocked/no selected route",
        )

    # BOD-104 final selected_route is authoritative for bind/receipt/launch.
    # Never replace it with a post-optimize STAY route that can diverge from
    # ep_decision.selected_route (session continuity already constrained ranking).
    selected_route = ep_decision.selected_route
    session_label, session_rewrite = _reconcile_session_label_with_ep(
        session_decision, selected_route=selected_route, fresh=session_state is None
    )

    if hooks.bind_prime_target is None:
        raise ControllerLaunchError(
            "missing_prime_binding",
            "controller selection requires bind_prime_target backed by trusted "
            "ConcreteRoute -> PrimeLaunchTarget evidence; default identity copy is forbidden",
        )
    prime_target = hooks.bind_prime_target(selected_route)
    _reject_identity_shaped_model(
        prime_target.prime_model, "prime_model", gateway=selected_route.gateway
    )
    if override is not None:
        if (
            prime_target.prime_provider != override.provider
            or prime_target.prime_model != override.model
        ):
            raise ControllerLaunchError(
                "override_target_mismatch",
                "selected Prime target must match explicit CLI override provider/model",
            )
        if override.reasoning_effort is not None:
            if prime_target.reasoning_effort is None:
                raise ControllerLaunchError(
                    "unsupported_reasoning",
                    "override requested reasoning_effort but target has no supported evidence",
                )
            if override.reasoning_effort != prime_target.reasoning_effort:
                raise ControllerLaunchError(
                    "reasoning_mismatch",
                    "override reasoning_effort must match supported target evidence exactly",
                )
            # Preserve exact supported effort on the target already.
        # Record only CLI provenance via decide_controller_launch below.

    digests, context_plan, context_pack, context_receipt = _selected_context_artifacts(
        ep_decision, prepared, compiler=hooks.compile_context_digests
    )

    store = hooks.receipt_store
    if store is None:
        root = Path(hooks.workspace_root) if hooks.workspace_root is not None else None
        store = default_receipt_store(root)

    selected_identity = {
        "gateway": selected_route.gateway,
        "provider": selected_route.provider,
        "model": selected_route.model,
        "resource_pool": selected_route.credential_pool or "default",
        "route_id": selected_route.route_id,
    }
    pool_ref = "pool://none"
    if prepared.pool_receipt is not None:
        pool_ref = f"pool://{getattr(prepared.pool_receipt, 'shortlist_digest', 'live')}"

    extension_session = {
        "mission_id": mission.mission_id,
        "attempt_id": mission.attempt_id,
        "session_decision": session_label,
        "context_plan_digest": digests["context_plan_digest"],
        "context_pack_digest": digests["context_pack_digest"],
        "context_receipt_digest": digests["context_receipt_digest"],
        "selected_prompt_digest": digests["selected_prompt_digest"],
        "prime_provider": prime_target.prime_provider,
        "prime_model": prime_target.prime_model,
        "execution_path_decision_digest": ep_decision.decision_digest,
    }
    if session_rewrite is not None:
        extension_session["session_decision_rewrite"] = session_rewrite

    try:
        receipt = hooks.build_receipt(
            execution_path_decision=ep_decision,
            selected_identity=selected_identity,
            story_id=mission.story_id,
            work_unit_id=mission.mission_id,
            attempt_id=mission.attempt_id,
            state="in_progress",
            context_plan=context_plan,
            context_pack=context_pack,
            context_receipt=context_receipt,
            extensions={"controller_launch": extension_session},
        )
        record = hooks.persist_receipt(store, receipt)
    except ControllerLaunchError:
        raise
    except Exception as exc:
        raise ControllerLaunchError(
            "receipt_persist_failed", f"failed to persist RoutingReceiptV1 before launch: {exc}"
        ) from exc

    receipt_ref = getattr(record, "receipt_id", None) or getattr(receipt, "receipt_id", None)
    if not receipt_ref:
        raise ControllerLaunchError(
            "receipt_persist_failed", "persisted routing receipt missing receipt_id"
        )
    receipt_ref = f"receipt://{receipt_ref}"

    evidence_refs = [
        f"execution_path://{ep_decision.decision_digest}",
        f"context_plan://{digests['context_plan_digest']}",
        pool_ref,
    ]
    if prepared.pool_receipt is not None and getattr(
        prepared.pool_receipt, "evidence_digest", None
    ):
        evidence_refs.append(f"pool_evidence://{prepared.pool_receipt.evidence_digest}")

    expiry = (when + hooks.decision_ttl).isoformat()
    freshness = {
        "decision_at": when.isoformat(),
        "execution_path": ep_decision.decision_digest,
        "context_plan": digests["context_plan_digest"],
    }
    # Merge EP freshness when present.
    if isinstance(ep_decision.freshness, Mapping):
        for key, value in ep_decision.freshness.items():
            freshness[str(key)] = str(value)

    selected_upstream = (
        f"{selected_route.gateway}/{selected_route.route_id}"
        if not selected_route.route_id.startswith(f"{selected_route.gateway}/")
        else selected_route.route_id
    )
    _reject_auto_identity(selected_upstream, "selected_upstream_route")

    why = ep_decision.why_selected or "bod104_selected"
    if session_decision is not None:
        why = f"{why}; session={session_label}:{session_decision.reason}"
    if session_rewrite is not None:
        why = f"{why}; {session_rewrite}"

    persisted = PersistedAuthoritativeDecision(
        execution_path_decision_digest=ep_decision.decision_digest,
        selected_upstream_route=selected_upstream,
        prime_target=prime_target,
        context_plan_digest=digests["context_plan_digest"],
        context_pack_digest=digests["context_pack_digest"],
        context_receipt_digest=digests["context_receipt_digest"],
        selected_prompt_digest=digests["selected_prompt_digest"],
        routing_receipt_ref=receipt_ref,
        pool_ref=pool_ref,
        evidence_refs=tuple(evidence_refs),
        constituent_freshness=freshness,
        minimum_expiry=expiry,
        session_decision=session_label,
        why_selected=why,
        task_profile_digest=mission.task_profile_digest or f"tp:{mission.attempt_id}",
        task_slice_digest=mission.task_slice_digest or prepared.task_slice.slice_id,
        trajectory_digest=mission.trajectory_digest or prepared.trajectory_id,
    )
    return decide_controller_launch(mission, persisted=persisted, override=override, now=when)


@dataclass
class InjectableControllerSelector:
    """Concrete ``ControllerSelector`` wrapping ``ControllerSelectionHooks``."""

    hooks: ControllerSelectionHooks

    def select_controller_launch(
        self,
        mission: ControllerMission,
        *,
        override: OperatorOverride | None = None,
        session_state: SessionState | None = None,
        now: datetime | None = None,
    ) -> ControllerLaunchDecision:
        return select_controller_launch(
            mission, hooks=self.hooks, override=override, session_state=session_state, now=now
        )


@dataclass(frozen=True)
class CompiledControllerPrompt:
    """Exact compiled prompt artifact for supervisor wiring."""

    route_id: str
    plan_digest: str
    pack_digest: str
    receipt_digest: str
    prompt_digest: str
    compiled_prompt: str


@dataclass
class ProductionControllerSelectionArtifacts:
    """Factory-owned mutable surface for compiled prompt lookup after selection."""

    compiled_by_prompt_digest: dict[str, CompiledControllerPrompt] = field(default_factory=dict)
    compiled_by_route_id: dict[str, CompiledControllerPrompt] = field(default_factory=dict)
    last_compiled: CompiledControllerPrompt | None = None

    def record(self, artifact: CompiledControllerPrompt) -> None:
        self.compiled_by_prompt_digest[artifact.prompt_digest] = artifact
        self.compiled_by_route_id[artifact.route_id] = artifact
        self.last_compiled = artifact


@dataclass(frozen=True)
class ProductionControllerSelectionBundle:
    """Production hooks plus the compiled-prompt artifact map."""

    hooks: ControllerSelectionHooks
    artifacts: ProductionControllerSelectionArtifacts


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prompt_digest_for_bytes(prompt: str | bytes) -> str:
    raw = prompt.encode("utf-8") if isinstance(prompt, str) else prompt
    return f"sha256:{_sha256_hex(raw)}"


def _require_mapping_target(
    route: ConcreteRoute, target_map: Mapping[str, PrimeLaunchTarget]
) -> PrimeLaunchTarget:
    target = target_map.get(route.route_id)
    if target is None:
        # Allow exact provider/model composite keys as alternate evidence ids.
        target = target_map.get(f"{route.provider}/{route.model}")
    if target is None:
        raise ControllerLaunchError(
            "missing_prime_binding",
            f"no trusted PrimeLaunchTarget mapping for route {route.route_id!r}",
        )
    if target.upstream_provider != route.provider or target.upstream_model != route.model:
        raise ControllerLaunchError(
            "prime_binding_mismatch",
            "trusted PrimeLaunchTarget upstream identity must match selected ConcreteRoute",
        )
    _reject_auto_identity(target.prime_provider, "prime_provider")
    _reject_identity_shaped_model(target.prime_model, "prime_model", gateway=route.gateway)
    _reject_identity_shaped_model(target.upstream_model, "upstream_model", gateway=route.gateway)
    return target


def _passport_is_live_healthy(passport: Any, *, now: datetime) -> bool:
    auth = getattr(passport, "auth_state", None)
    availability = getattr(passport, "availability_state", None)
    expires_at = getattr(passport, "expires_at", None)
    if auth != "authorized" or availability != "eligible":
        return False
    return not (expires_at is not None and expires_at <= now)


def _metadata_price_for_identity(
    metadata: Any, identity_id: str, *, now: datetime, evidence_id: str
) -> PriceEvidenceInput | None:
    if metadata is None:
        return None
    index = metadata.index_omniroute() if hasattr(metadata, "index_omniroute") else {}
    record = index.get(identity_id)
    if record is None:
        return None
    caps = getattr(record, "caps", None)
    if caps is None:
        return None
    input_field = caps.field("input_cost_per_million") if hasattr(caps, "field") else None
    output_field = caps.field("output_cost_per_million") if hasattr(caps, "field") else None
    if input_field is None and output_field is None:
        return None
    price: PriceEvidenceInput = {"evidence_id": evidence_id, "observed_at": now.isoformat()}
    if input_field is not None:
        price["input_usd_per_mtok"] = str(input_field.value)
    if output_field is not None:
        price["output_usd_per_mtok"] = str(output_field.value)
    return price


def _price_from_passport(
    passport: Any, *, now: datetime, evidence_id: str
) -> PriceEvidenceInput | None:
    cost = getattr(passport, "token_cost_per_1k", None)
    if cost is None:
        return None
    # Passport stores USD per 1k tokens; convert to per-million for ledger.
    per_mtok = float(cost) * 1000.0
    return {
        "input_usd_per_mtok": str(per_mtok),
        "output_usd_per_mtok": str(per_mtok),
        "observed_at": now.isoformat(),
        "evidence_id": evidence_id,
    }


def _context_plan_from_requirements(reqs: Mapping[str, Any], *, candidate_id: str) -> Any:
    from verdict.context_pack import ContextPlan

    if isinstance(reqs, ContextPlan):
        return reqs
    payload = dict(reqs)
    payload.setdefault("candidate_id", candidate_id)
    if "plan_id" not in payload or not str(payload.get("plan_id") or "").strip():
        raise ControllerLaunchError(
            "missing_context_plan",
            f"selected offer {candidate_id!r} lacks ContextPlan plan_id for compilation",
        )
    try:
        return ContextPlan.from_dict(payload)
    except Exception as exc:
        # Fallback for estimate-shaped dicts already carrying digest/budget fields.
        try:
            return ContextPlan(
                plan_id=str(payload["plan_id"]),
                candidate_id=str(payload.get("candidate_id") or candidate_id),
                token_budget=int(payload.get("token_budget") or 4096),
                output_token_reserve=int(payload.get("output_token_reserve") or 0),
                tool_token_reserve=int(payload.get("tool_token_reserve") or 0),
                estimated_input_tokens=payload.get("estimated_input_tokens"),
                candidate_context_window=payload.get("candidate_context_window"),
                required_slot_types=tuple(payload.get("required_slot_types") or ()),
                created_at=str(payload.get("created_at") or _utc_now().isoformat()),
            )
        except Exception as inner:
            raise ControllerLaunchError(
                "invalid_context_plan",
                f"cannot reconstruct ContextPlan for {candidate_id!r}: {exc}; {inner}",
            ) from inner


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + _sha256_hex(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    )


def _default_controller_context_units(
    mission: ControllerMission,
    decision: ExecutionPathDecision,
    prepared: ExecutionPathRequest,
    plan: Any,
) -> tuple[Any, ...]:
    from verdict.context_pack import ContextUnit

    units: list[Any] = []
    observed = (prepared.now or _utc_now()).isoformat()
    objective = mission.objective
    units.append(
        ContextUnit(
            unit_id=f"mission:{mission.mission_id}",
            slot_type="instructions",
            key="controller.objective",
            content=objective,
            source_uri=f"mission://{mission.mission_id}",
            source_digest=_canonical_digest(
                {"mission_id": mission.mission_id, "objective": objective}
            ),
            observed_at=observed,
            trust="verified",
            authority="controller_mission",
            status="active",
        )
    )
    for ref in mission.durable_context_refs:
        units.append(
            ContextUnit(
                unit_id=f"durable:{ref}",
                slot_type="state",
                key=f"durable:{ref}",
                content=f"durable context ref: {ref}",
                source_uri=str(ref),
                source_digest=_canonical_digest({"ref": ref}),
                observed_at=observed,
                trust="verified",
                authority="durable_context",
                status="active",
            )
        )
    if mission.proof_burden:
        units.append(
            ContextUnit(
                unit_id=f"proof:{mission.attempt_id}",
                slot_type="policy",
                key="controller.proof_burden",
                content=str(mission.proof_burden),
                source_uri=f"proof://{mission.attempt_id}",
                source_digest=_canonical_digest({"proof": mission.proof_burden}),
                observed_at=observed,
                trust="verified",
                authority="controller_mission",
                status="active",
            )
        )
    # Keep plan candidate binding visible without inventing external retrieval.
    units.append(
        ContextUnit(
            unit_id=f"plan:{plan.plan_id}",
            slot_type="receipt",
            key="controller.selected_plan",
            content=(
                f"selected_route={decision.selected_candidate_id}; "
                f"plan_id={plan.plan_id}; digest={plan.digest}"
            ),
            source_uri=f"context-plan://{plan.plan_id}",
            source_digest=plan.digest,
            observed_at=observed,
            trust="verified",
            authority="bod143_context_plan",
            status="active",
        )
    )
    return tuple(units)


def _metadata_record_for_identity(metadata: Any, identity_id: str) -> Any | None:
    if metadata is None:
        return None
    index = metadata.index_omniroute() if hasattr(metadata, "index_omniroute") else {}
    if not isinstance(index, Mapping):
        return None
    return index.get(identity_id)


def _metadata_cap_bool(record: Any, name: str) -> bool | None:
    caps = getattr(record, "caps", None) if record is not None else None
    if caps is None or not hasattr(caps, "field"):
        return None
    try:
        field = caps.field(name)
    except Exception:
        return None
    if field is None:
        return None
    value = getattr(field, "value", None)
    return value if isinstance(value, bool) else None


def _metadata_context_window(record: Any) -> int | None:
    caps = getattr(record, "caps", None) if record is not None else None
    if caps is None or not hasattr(caps, "field"):
        return None
    for name in ("context", "max_input"):
        try:
            field = caps.field(name)
        except Exception:
            continue
        if field is None:
            continue
        value = getattr(field, "value", None)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and int(value) > 0:
            return int(value)
    return None


def _controller_seed_requirements(
    mission: ControllerMission, *, passport: Any, metadata_record: Any | None
) -> Any:
    """Build TaskRequirements only from genuine passport/metadata evidence."""
    from verdict.capability_gate import TaskRequirements

    names: list[str] = []
    # Passport tool_support is explicit live evidence when True.
    if (
        bool(getattr(passport, "tool_support", False))
        or _metadata_cap_bool(metadata_record, "tools") is True
    ):
        names.append("tools")
    # Prefer explicit True metadata caps; never invent False as evidence.
    for cap_name in ("structured", "vision", "attachment", "reasoning"):
        if _metadata_cap_bool(metadata_record, cap_name) is True and cap_name not in names:
            names.append(cap_name)

    min_context: int | None = None
    passport_window = getattr(passport, "context_window", None)
    if isinstance(passport_window, int) and passport_window > 0:
        min_context = passport_window
    else:
        meta_window = _metadata_context_window(metadata_record)
        if meta_window is not None:
            min_context = meta_window

    reasons = ("controller_seed_requirements_from_passport_metadata",)
    return TaskRequirements(names=tuple(names), min_context=min_context, reasons=reasons)


def _controller_seed_candidate_evidence(
    *,
    route_id: str,
    passport: Any,
    metadata_record: Any | None,
    evidence_digest: str,
    when: datetime,
) -> Any:
    """Assemble CandidateCapabilityEvidence without inventing unknown caps as False."""
    from verdict.effective_capability import CandidateCapabilityEvidence

    intrinsic: dict[str, bool | None] = {}
    if bool(getattr(passport, "tool_support", False)):
        intrinsic["tools"] = True
    else:
        tools_meta = _metadata_cap_bool(metadata_record, "tools")
        if tools_meta is not None:
            intrinsic["tools"] = tools_meta

    for cap_name in ("structured", "vision", "attachment", "reasoning"):
        value = _metadata_cap_bool(metadata_record, cap_name)
        if value is not None:
            intrinsic[cap_name] = value

    context_window: int | None = None
    passport_window = getattr(passport, "context_window", None)
    if isinstance(passport_window, int) and passport_window > 0:
        context_window = passport_window
    else:
        context_window = _metadata_context_window(metadata_record)

    # Require at least one genuine capability fact; empty intrinsics + no window
    # cannot plan effective capability without inventing sufficiency.
    if not intrinsic and context_window is None:
        raise ControllerLaunchError(
            "missing_effective_capability_evidence",
            f"route {route_id!r} lacks passport/metadata intrinsic or context-window evidence",
        )

    return CandidateCapabilityEvidence(
        candidate_id=route_id,
        intrinsic_capabilities=intrinsic,
        evidence_digest=evidence_digest,
        context_window=context_window,
        hard_gate_excluded=False,
        hard_gate_reason=None,
        observed_at=when.isoformat(),
        freshness="fresh",
    )


def _controller_seed_context_snapshot(
    mission: ControllerMission, *, evidence_digest: str, when: datetime
) -> Any:
    """Seed-time ContextEvidenceSnapshot from mission durable refs only."""
    from verdict.effective_capability import ContextEvidenceSnapshot

    slots = {"instructions"}
    evidence_keys = {"mission.objective"}
    if mission.durable_context_refs:
        slots.add("state")
        for ref in mission.durable_context_refs:
            evidence_keys.add(f"durable:{ref}")
    if mission.proof_burden:
        slots.add("policy")
        evidence_keys.add("controller.proof_burden")
    # Controller seeds always carry objective + attempt identity as instructions.
    used = max(64, min(512, 40 + len(mission.objective) // 4))
    return ContextEvidenceSnapshot(
        available_slots=frozenset(slots),
        available_evidence_keys=frozenset(evidence_keys),
        omitted_required=frozenset(),
        token_budget=4096,
        used_tokens=used,
        evidence_digest=evidence_digest,
        observed_at=when.isoformat(),
        fresh=True,
        hydrated=True,
    )


def _controller_seed_tool_snapshot(
    mission: ControllerMission, *, evidence_digest: str, when: datetime
) -> Any:
    from verdict.effective_capability import ToolSurfaceSnapshot

    required = tuple(str(t).strip() for t in mission.required_tools if str(t).strip())
    # Required MCP ids are additional tool-surface capabilities when declared.
    required_mcp = tuple(str(t).strip() for t in mission.required_mcp if str(t).strip())
    available = frozenset([*required, *required_mcp])
    surface = {cap: f"controller.{cap}" for cap in sorted(available)}
    return ToolSurfaceSnapshot(
        available_capabilities=available,
        selected_surface=surface,
        evidence_digest=evidence_digest,
        observed_at=when.isoformat(),
        fresh=True,
    )


def _controller_seed_required_slots(mission: ControllerMission) -> tuple[str, ...]:
    slots = ["instructions"]
    if mission.durable_context_refs:
        slots.append("state")
    if mission.proof_burden:
        slots.append("policy")
    return tuple(slots)


def _controller_seed_required_evidence(mission: ControllerMission) -> tuple[str, ...]:
    keys = ["mission.objective"]
    for ref in mission.durable_context_refs:
        keys.append(f"durable:{ref}")
    if mission.proof_burden:
        keys.append("controller.proof_burden")
    return tuple(keys)


def _enrich_seed_context_plan_requirements(plan: Any, *, route_id: str, when: datetime) -> Any:
    """Ensure ContextPlan reconstruction fields exist without inventing sufficiency."""
    from dataclasses import replace as _replace

    reqs = dict(plan.context_plan_requirements or {})
    reqs.setdefault("candidate_id", route_id)
    if "plan_id" not in reqs or not str(reqs.get("plan_id") or "").strip():
        reqs["plan_id"] = f"seed:{plan.plan_id}"
    if "created_at" not in reqs:
        reqs["created_at"] = when.isoformat()
    if "token_budget" not in reqs or reqs.get("token_budget") in (None, ""):
        reqs["token_budget"] = 4096
    if "required_slot_types" not in reqs:
        reqs["required_slot_types"] = list(plan.required_context_slots)
    return _replace(plan, context_plan_requirements=reqs)


def _resolve_seed_certification(
    *, route_id: str, identity_id: str, certification_by_id: Mapping[str, tuple[Any, str]] | None
) -> tuple[Any, str]:
    """Require per-route runtime certification evidence; never default UNKNOWN."""
    from verdict.runtime_certification import CertificationState

    if not certification_by_id:
        raise ControllerLaunchError(
            "missing_certification_evidence",
            f"route {route_id!r} has no runtime certification evidence map",
        )
    entry = certification_by_id.get(route_id)
    if entry is None:
        entry = certification_by_id.get(identity_id)
    if entry is None:
        raise ControllerLaunchError(
            "missing_certification_evidence",
            f"route {route_id!r} missing per-route runtime certification evidence",
        )
    cert_state, cert_freshness = entry
    if not isinstance(cert_state, CertificationState):
        try:
            cert_state = CertificationState(str(cert_state))
        except Exception as exc:
            raise ControllerLaunchError(
                "invalid_certification_evidence",
                f"route {route_id!r} certification_state is not a CertificationState: {exc}",
            ) from exc
    freshness = str(cert_freshness or "").strip().lower()
    if not freshness:
        raise ControllerLaunchError(
            "missing_certification_freshness",
            f"route {route_id!r} certification freshness is empty",
        )
    # UNKNOWN remains a real evidence value when callers supply it; seeds must
    # not invent it as a missing-map fallback.
    return cert_state, freshness


def _health_claim_for_passport(passport: Any) -> str:
    """Map ModelPassport fields to BOD-92 health_claim vocabulary.

    ``passport_from_probe`` only writes ``auth_state="authorized"`` with
    ``availability_state="eligible"`` after a live at-rest probe reported
    ``ready``. That combination is therefore evidence-backed readiness, not an
    invented promotion. Passport availability itself never stores the literal
    ``ready`` token.
    """
    availability = getattr(passport, "availability_state", None)
    auth = getattr(passport, "auth_state", None)
    if auth == "authorized" and availability == "eligible":
        return "ready"
    if availability == "degraded":
        return "degraded"
    if availability in {"quarantined", "denied"}:
        return "unavailable"
    if availability == "ready":
        # Defensive only: passports never persist this literal.
        return "ready"
    return "unknown"


def _certify_controller_passports(
    healthy_passports: Mapping[str, Any], *, now: datetime, certify_runtime_fn: RuntimeCertifyFn
) -> Mapping[str, tuple[CertificationState, str]]:
    """Translate passport claims into BOD-92 evidence without probes or promotion.

    Component ids are the passport inventory keys, not model leaf names or
    filesystem paths. The seed resolver falls back from normalized route_id to
    this exact identity_id. Only matching PROVIDER components from the report
    count (BOD-92 has no MODEL kind for passport rows).
    """
    try:
        snapshots: list[DetectedSnapshot] = []
        for identity_id, passport in sorted(healthy_passports.items()):
            observed = getattr(passport, "last_verified_timestamp", None)
            if observed is None:
                observed = getattr(passport, "qualified_at", None)
            snapshots.append(
                DetectedSnapshot(
                    component_id=identity_id,
                    identity=identity_id,
                    kind=ComponentKind.PROVIDER,
                    source="controller_passport",
                    health_claim=_health_claim_for_passport(passport),
                    observed_at=observed,
                    requires_probe=False,
                    premium_probe=False,
                )
            )
        report = certify_runtime_fn(
            snapshots=tuple(snapshots), now=now, run_registered_detectors=False
        )
        return {
            component.component_id: (component.state, component.freshness)
            for component in report.components
            if component.component_id in healthy_passports
            and component.identity == component.component_id
            and component.kind is ComponentKind.PROVIDER
        }
    except Exception as exc:
        # Exception text can contain private paths or credentials. Keep the
        # externally visible error fixed while retaining the cause for debugging.
        raise ControllerLaunchError(
            "runtime_certification_failed",
            "could not build offline passport runtime certification evidence",
        ) from exc


def build_evidence_backed_seed_offers(
    mission: ControllerMission,
    when: datetime,
    *,
    healthy_passports: Mapping[str, Any],
    metadata_snapshot: Any | None = None,
    free_identity_ids: frozenset[str] | None = None,
    price_evidence_by_id: Mapping[str, PriceEvidenceInput] | None = None,
    certification_by_id: Mapping[str, tuple[Any, str]] | None = None,
    gateway: str = "omniroute",
    execution_tokens: int = 8_000,
    capability_evidence_by_id: Mapping[str, Any] | None = None,
    context_evidence_by_id: Mapping[str, Any] | None = None,
    tool_surface_by_id: Mapping[str, Any] | None = None,
    task_requirements_by_id: Mapping[str, Any] | None = None,
) -> tuple[ExecutionPathOffer, ...]:
    """Build seed offers only from genuine passport/metadata/price/cert evidence.

    Seeds are inventory/cost candidates: ``ConcreteRoute.eligible`` stays
    ``False`` until ``prepare_controller_execution_request`` confirms.

    Assistance plans come from ``plan_effective_capability`` +
    ``build_strategy_from_assistance`` only. Canned ``result="sufficient"`` /
    ``intrinsic_sufficient=True`` authority is forbidden. Per-route runtime
    certification evidence is required; UNKNOWN is never invented as a missing
    fallback. Identities without price/free admission, certification, or
    assemblable effective-capability evidence are omitted or fail closed with a
    named reason. Empty result is a caller concern.
    """
    from verdict.effective_capability import plan_effective_capability
    from verdict.expected_cost import build_strategy_from_assistance

    if not healthy_passports:
        raise ControllerLaunchError(
            "missing_healthy_passports",
            "production seed offers require non-empty healthy passport evidence",
        )
    if certification_by_id is None:
        raise ControllerLaunchError(
            "missing_certification_evidence",
            "production seed offers require per-route runtime certification evidence",
        )

    task_slice = _mission_task_slice(mission)
    trajectory_id = mission.trajectory_digest or f"controller:{mission.attempt_id}"
    free_ids = free_identity_ids or frozenset()
    offers: list[ExecutionPathOffer] = []
    capability_failures: list[str] = []
    certification_failures: list[str] = []

    for identity_id, passport in sorted(healthy_passports.items()):
        if not _passport_is_live_healthy(passport, now=when):
            continue
        try:
            provider, leaf_model, route_id = _normalize_passport_identity(
                identity_id, passport, gateway=gateway
            )
        except ControllerLaunchError:
            continue

        evidence_payload = {
            "identity_id": identity_id,
            "provider": provider,
            "model_id": leaf_model,
            "auth_state": getattr(passport, "auth_state", None),
            "availability_state": getattr(passport, "availability_state", None),
            "qualified_at": str(getattr(passport, "qualified_at", "")),
            "expires_at": str(getattr(passport, "expires_at", "")),
        }
        evidence_digest = "sha256:" + _sha256_hex(
            json.dumps(
                evidence_payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        )

        is_free = route_id in free_ids or identity_id in free_ids
        price: PriceEvidenceInput | None = None
        if price_evidence_by_id and (
            route_id in price_evidence_by_id or identity_id in price_evidence_by_id
        ):
            price = price_evidence_by_id.get(route_id) or price_evidence_by_id[identity_id]
        if price is None:
            price = _metadata_price_for_identity(
                metadata_snapshot, route_id, now=when, evidence_id=evidence_digest
            )
        if price is None:
            price = _metadata_price_for_identity(
                metadata_snapshot, identity_id, now=when, evidence_id=evidence_digest
            )
        if price is None:
            price = _price_from_passport(passport, now=when, evidence_id=evidence_digest)
        if is_free and price is None:
            price = {
                "input_usd_per_mtok": "0",
                "output_usd_per_mtok": "0",
                "observed_at": when.isoformat(),
                "evidence_id": evidence_digest,
            }
        if price is None:
            # No genuine price and not free-admitted: omit rather than invent.
            continue

        try:
            cert_state, cert_freshness = _resolve_seed_certification(
                route_id=route_id, identity_id=identity_id, certification_by_id=certification_by_id
            )
        except ControllerLaunchError as exc:
            certification_failures.append(f"{route_id}:{exc.reason_code}")
            continue

        metadata_record = _metadata_record_for_identity(
            metadata_snapshot, route_id
        ) or _metadata_record_for_identity(metadata_snapshot, identity_id)

        try:
            if capability_evidence_by_id and (
                route_id in capability_evidence_by_id or identity_id in capability_evidence_by_id
            ):
                candidate = (
                    capability_evidence_by_id.get(route_id)
                    or capability_evidence_by_id[identity_id]
                )
            else:
                candidate = _controller_seed_candidate_evidence(
                    route_id=route_id,
                    passport=passport,
                    metadata_record=metadata_record,
                    evidence_digest=evidence_digest,
                    when=when,
                )

            if task_requirements_by_id and (
                route_id in task_requirements_by_id or identity_id in task_requirements_by_id
            ):
                requirements = (
                    task_requirements_by_id.get(route_id) or task_requirements_by_id[identity_id]
                )
            else:
                requirements = _controller_seed_requirements(
                    mission, passport=passport, metadata_record=metadata_record
                )

            if context_evidence_by_id and (
                route_id in context_evidence_by_id or identity_id in context_evidence_by_id
            ):
                context_snap = (
                    context_evidence_by_id.get(route_id) or context_evidence_by_id[identity_id]
                )
            else:
                context_snap = _controller_seed_context_snapshot(
                    mission, evidence_digest=evidence_digest, when=when
                )

            if tool_surface_by_id and (
                route_id in tool_surface_by_id or identity_id in tool_surface_by_id
            ):
                tools_snap = tool_surface_by_id.get(route_id) or tool_surface_by_id[identity_id]
            else:
                tools_snap = _controller_seed_tool_snapshot(
                    mission, evidence_digest=evidence_digest, when=when
                )

            req_slots = _controller_seed_required_slots(mission)
            req_evidence = _controller_seed_required_evidence(mission)
            req_tools = tuple(str(t).strip() for t in mission.required_tools if str(t).strip())

            plan = plan_effective_capability(
                task_slice=task_slice,
                candidate=candidate,
                requirements=requirements,
                context=context_snap,
                tools=tools_snap,
                required_context_slots=req_slots,
                required_context_evidence=req_evidence,
                required_tool_capabilities=req_tools,
            )
            plan = _enrich_seed_context_plan_requirements(plan, route_id=route_id, when=when)
        except ControllerLaunchError as exc:
            capability_failures.append(f"{route_id}:{exc.reason_code}")
            continue
        except Exception as exc:
            capability_failures.append(f"{route_id}:effective_capability_failed:{exc}")
            continue

        # Seeds are inventory/cost candidates only. eligible=True is authority and
        # must wait for prepare_controller_execution_request confirmation (M1).
        route = ConcreteRoute(
            route_id=route_id,
            gateway=gateway,
            provider=provider,
            model=leaf_model,
            credential_pool=None,
            capability_tier=2,
            eligible=False,
            excluded=False,
        )
        expected = build_strategy_from_assistance(
            strategy_id=f"direct_cheap:{route_id}",
            trajectory_id=trajectory_id,
            assistance=plan.assistance_cost,
            execution_tokens=execution_tokens,
            price=price,
            is_free=is_free,
            qualified=True,
            free_first_preferred=True,
            now=when,
        )
        offers.append(
            ExecutionPathOffer(
                strategy="direct_cheap",
                route=route,
                assistance_plan=plan,
                expected_cost=expected,
                certification_state=cert_state,
                certification_freshness=cert_freshness,
                is_cheap=is_free,
                is_paid=not is_free,
            )
        )

    if not offers:
        if certification_failures and not capability_failures:
            raise ControllerLaunchError(
                "missing_certification_evidence",
                "no seed offers: per-route runtime certification evidence missing or invalid "
                f"({'; '.join(certification_failures[:5])})",
            )
        if capability_failures and not certification_failures:
            raise ControllerLaunchError(
                "missing_effective_capability_evidence",
                "no seed offers: effective-capability evidence could not be assembled "
                f"({'; '.join(capability_failures[:5])})",
            )
        if certification_failures or capability_failures:
            raise ControllerLaunchError(
                "seed_evidence_incomplete",
                "no seed offers after fail-closed certification/effective-capability checks "
                f"(cert={'; '.join(certification_failures[:3])}; "
                f"cap={'; '.join(capability_failures[:3])})",
            )

    return tuple(offers)


def build_production_controller_selection_hooks(
    *,
    intelligence_service: Any | None = None,
    prepare_execution_request: PrepareControllerFn | None = None,
    seed_offers: Callable[[ControllerMission, datetime], Sequence[ExecutionPathOffer]]
    | None = None,
    bind_prime_target: BindTargetFn | None = None,
    prime_target_map: Mapping[str, PrimeLaunchTarget] | None = None,
    load_prime_target_map: Callable[[], Mapping[str, PrimeLaunchTarget]] | None = None,
    compile_context_digests: Callable[
        [ExecutionPathDecision, ExecutionPathRequest], Mapping[str, Any]
    ]
    | None = None,
    context_compiler: Any | None = None,
    context_units_for_decision: Callable[
        [ControllerMission, ExecutionPathDecision, ExecutionPathRequest, Any], Sequence[Any]
    ]
    | None = None,
    healthy_passports: Mapping[str, Any] | None = None,
    passport_store_path: Path | str | None = None,
    load_healthy_passports_fn: Callable[[Path | None], Mapping[str, Any]] | None = None,
    metadata_snapshot: Any | None = None,
    metadata_store_path: Path | str | None = None,
    load_metadata_fn: Callable[[Path | str | None], Any] | None = None,
    admit_snapshot: Any | None = None,
    free_identity_ids: frozenset[str] | None = None,
    price_evidence_by_id: Mapping[str, PriceEvidenceInput] | None = None,
    certification_by_id: Mapping[str, tuple[Any, str]] | None = None,
    certify_runtime_fn: RuntimeCertifyFn = certify_runtime,
    optimize: OptimizeFn = optimize_execution_path,
    decide_session: SessionDecideFn = decide_session_route,
    build_receipt: BuildReceiptFn = build_routing_receipt,
    persist_receipt: PersistReceiptFn = persist_routing_receipt,
    receipt_store: Any | None = None,
    workspace_root: Path | str | None = None,
    criticality: str = "high",
    decision_ttl: timedelta = timedelta(hours=1),
    cost_state_factory: Callable[[ConcreteRoute, ConcreteRoute, datetime], CostState] | None = None,
    task_state_factory: Callable[[ControllerMission], TaskState] | None = None,
    gateway: str = "omniroute",
    require_live_sources: bool = True,
) -> ProductionControllerSelectionBundle:
    """Build production ``ControllerSelectionHooks`` from genuine evidence only.

    Explicit dependency parameters are intentional so tests can inject live
    fixtures. The default load path reads healthy passports and Core metadata
    from disk, builds evidence-backed seed offers, and wires
    ``IntelligenceService.prepare_controller_execution_request``.

    Fails closed with named ``ControllerLaunchError`` when required live
    sources, Prime binding evidence, or a real context compiler are absent.
    Never fabricates offers, prices, pack digests, or identity-copy Prime binds.
    """
    artifacts = ProductionControllerSelectionArtifacts()
    mission_holder: dict[str, ControllerMission | None] = {"mission": None}

    # --- Prime binding (required; no default identity copy) ---
    resolved_bind = bind_prime_target
    resolved_map: Mapping[str, PrimeLaunchTarget] | None = prime_target_map
    if resolved_bind is None:
        if resolved_map is None and load_prime_target_map is not None:
            try:
                resolved_map = load_prime_target_map()
            except ControllerLaunchError:
                raise
            except Exception as exc:
                raise ControllerLaunchError(
                    "prime_binding_load_failed",
                    f"failed to load trusted PrimeLaunchTarget map: {exc}",
                ) from exc
        if resolved_map is None or not resolved_map:
            raise ControllerLaunchError(
                "missing_prime_binding",
                "production factory requires explicit prime_target_map / "
                "load_prime_target_map / bind_prime_target; "
                "default identity copy is forbidden",
            )
        target_map = dict(resolved_map)

        def resolved_bind(route: ConcreteRoute) -> PrimeLaunchTarget:
            return _require_mapping_target(route, target_map)

    # --- Evidence sources for seed offers ---
    passports = healthy_passports
    if passports is None and seed_offers is None:
        loader = load_healthy_passports_fn
        if loader is None:
            from verdict.prove_at_rest import load_healthy_passports as _load_hp

            loader = _load_hp
        path = Path(passport_store_path) if passport_store_path is not None else None
        try:
            passports = dict(loader(path))
        except ControllerLaunchError:
            raise
        except Exception as exc:
            if require_live_sources:
                raise ControllerLaunchError(
                    "missing_healthy_passports", f"failed to load healthy passports: {exc}"
                ) from exc
            passports = {}
        if require_live_sources and not passports:
            raise ControllerLaunchError(
                "missing_healthy_passports",
                "production factory found no healthy passport evidence on disk",
            )

    metadata = metadata_snapshot
    if metadata is None and seed_offers is None:
        loader_meta = load_metadata_fn
        if loader_meta is None:
            from verdict.metadata import load_store as _load_store

            loader_meta = _load_store
        try:
            metadata = loader_meta(metadata_store_path)
        except ControllerLaunchError:
            raise
        except Exception as exc:
            if require_live_sources:
                raise ControllerLaunchError(
                    "missing_metadata", f"failed to load Core metadata snapshot: {exc}"
                ) from exc
            metadata = None
        if require_live_sources and metadata is None:
            raise ControllerLaunchError(
                "missing_metadata", "production factory requires a Core metadata snapshot"
            )

    free_ids = free_identity_ids
    if free_ids is None and admit_snapshot is not None:
        # Derive free evidence only from a genuine admit snapshot when provided.
        try:
            from verdict.free_tier_admit import admit_free_tier_active

            receipt = admit_free_tier_active(admit_snapshot)
            free_ids = frozenset(receipt.free_admitted or receipt.admitted or ())
        except Exception as exc:
            raise ControllerLaunchError(
                "admit_snapshot_unusable",
                f"failed to derive free-admission evidence from admit snapshot: {exc}",
            ) from exc

    if seed_offers is None:
        if passports is None:
            raise ControllerLaunchError(
                "missing_healthy_passports",
                "production seed offers require healthy passport evidence",
            )

        def seed_offers(mission: ControllerMission, when: datetime) -> Sequence[ExecutionPathOffer]:
            mission_holder["mission"] = mission
            certifications = certification_by_id
            if certifications is None:
                certifications = _certify_controller_passports(
                    passports, now=when, certify_runtime_fn=certify_runtime_fn
                )
            offers = build_evidence_backed_seed_offers(
                mission,
                when,
                healthy_passports=passports,
                metadata_snapshot=metadata,
                free_identity_ids=free_ids,
                price_evidence_by_id=price_evidence_by_id,
                certification_by_id=certifications,
                gateway=gateway,
            )
            if not offers:
                raise ControllerLaunchError(
                    "no_eligible_route",
                    "healthy passports produced no evidence-backed seed offers "
                    "(missing price/free admission evidence)",
                )
            return offers
    else:
        user_seed = seed_offers

        def seed_offers(mission: ControllerMission, when: datetime) -> Sequence[ExecutionPathOffer]:
            mission_holder["mission"] = mission
            return user_seed(mission, when)

    # --- prepare_execution_request ---
    resolved_prepare = prepare_execution_request
    if resolved_prepare is None:
        service = intelligence_service
        if service is None:
            raise ControllerLaunchError(
                "missing_intelligence_service",
                "production factory requires intelligence_service or "
                "prepare_execution_request; bare CLI cannot invent live eligibility",
            )
        if not hasattr(service, "prepare_controller_execution_request"):
            raise ControllerLaunchError(
                "missing_intelligence_service",
                "intelligence_service lacks prepare_controller_execution_request",
            )

        def resolved_prepare(
            task: str, criticality: str, context: dict[str, Any], request: ExecutionPathRequest
        ) -> ExecutionPathRequest:
            return cast(
                ExecutionPathRequest,
                service.prepare_controller_execution_request(task, criticality, context, request),
            )

    # --- context compiler (required; no placeholder digests) ---
    resolved_compile = compile_context_digests
    if resolved_compile is None:
        compiler = context_compiler
        if compiler is None:
            from verdict.context_pack import ContextPackCompiler

            compiler = ContextPackCompiler()
        units_factory = context_units_for_decision or (
            lambda mission, decision, prepared, plan: _default_controller_context_units(
                mission, decision, prepared, plan
            )
        )

        def resolved_compile(
            decision: ExecutionPathDecision, prepared: ExecutionPathRequest
        ) -> Mapping[str, str]:
            from verdict.context_pack import ContextReceipt

            selected_id = decision.selected_candidate_id
            if not selected_id:
                raise ControllerLaunchError(
                    "missing_context_plan",
                    "execution path decision has no selected_candidate_id for context compile",
                )
            selected_offer = _find_offer(prepared.offers, selected_id)
            if selected_offer is None:
                raise ControllerLaunchError(
                    "missing_context_plan",
                    f"selected candidate {selected_id!r} missing from prepared offers",
                )
            reqs = selected_offer.assistance_plan.context_plan_requirements or {}
            if not isinstance(reqs, Mapping) or not reqs:
                raise ControllerLaunchError(
                    "missing_context_plan",
                    f"selected offer {selected_id!r} has empty context_plan_requirements",
                )
            plan = _context_plan_from_requirements(reqs, candidate_id=selected_id)
            mission = mission_holder["mission"]
            if mission is None:
                raise ControllerLaunchError(
                    "missing_context_compiler_mission",
                    "context compile requires the active ControllerMission from seed_offers",
                )
            units = tuple(units_factory(mission, decision, prepared, plan))
            if not units:
                raise ControllerLaunchError(
                    "missing_context_units",
                    "context compiler received no ContextUnit evidence to compile",
                )
            try:
                pack = compiler.compile_units(units, plan)
            except Exception as exc:
                raise ControllerLaunchError(
                    "context_compile_failed",
                    f"ContextPackCompiler failed for {selected_id!r}: {exc}",
                ) from exc
            receipt = ContextReceipt.from_pack(pack)
            prompt = pack.compiled_prompt
            if not isinstance(prompt, str) or not prompt.strip():
                raise ControllerLaunchError(
                    "missing_compiled_prompt",
                    "context compiler produced empty compiled_prompt bytes",
                )
            prompt_digest = _prompt_digest_for_bytes(prompt)
            plan_digest = plan.digest
            pack_digest = pack.digest
            receipt_digest = receipt.digest
            artifacts.record(
                CompiledControllerPrompt(
                    route_id=selected_id,
                    plan_digest=plan_digest,
                    pack_digest=pack_digest,
                    receipt_digest=receipt_digest,
                    prompt_digest=prompt_digest,
                    compiled_prompt=prompt,
                )
            )
            return {
                "context_plan_digest": plan_digest,
                "context_pack_digest": pack_digest,
                "context_receipt_digest": receipt_digest,
                "selected_prompt_digest": prompt_digest,
                "context_plan": plan,
                "context_pack": pack,
                "context_receipt": receipt,
            }

    # M5: fail closed at factory build when continuity factories are absent.
    # Do not leave None defaults that only explode after session_state appears.
    if cost_state_factory is None or task_state_factory is None:
        raise ControllerLaunchError(
            "missing_session_hooks",
            "production factory requires cost_state_factory and task_state_factory; "
            "None defaults that explode only when session continuity is requested are forbidden",
        )

    hooks = ControllerSelectionHooks(
        prepare_execution_request=resolved_prepare,
        seed_offers=seed_offers,
        optimize=optimize,
        decide_session=decide_session,
        build_receipt=build_receipt,
        persist_receipt=persist_receipt,
        bind_prime_target=resolved_bind,
        receipt_store=receipt_store,
        workspace_root=workspace_root,
        criticality=criticality,
        decision_ttl=decision_ttl,
        cost_state_factory=cost_state_factory,
        task_state_factory=task_state_factory,
        compile_context_digests=resolved_compile,
    )
    return ProductionControllerSelectionBundle(hooks=hooks, artifacts=artifacts)


def build_production_controller_selection_bundle(
    **kwargs: Any,
) -> ProductionControllerSelectionBundle:
    """Alias for ``build_production_controller_selection_hooks``.

    Supervisor/tests may call this name when they want the hooks+artifacts
    bundle seam explicitly. Keyword arguments are identical.
    """
    return build_production_controller_selection_hooks(**kwargs)


__all__ = [
    "CompiledControllerPrompt",
    "ControllerSelectionHooks",
    "ControllerSelector",
    "InjectableControllerSelector",
    "ProductionControllerSelectionArtifacts",
    "ProductionControllerSelectionBundle",
    "RuntimeCertifyFn",
    "build_evidence_backed_seed_offers",
    "build_production_controller_selection_bundle",
    "build_production_controller_selection_hooks",
    "select_controller_launch",
]
