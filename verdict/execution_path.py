"""Execution-path optimizer — cheapest SAFE COMPLETE path (execution-path authority).

Integration layer only. Consumes existing contracts; does not reinvent
CandidatePool, EffectiveCapability, CostLedger, SessionEconomics,
BoundedRecovery, ContextBudget, ContextTrust, or RuntimeCertification.

Canonical loop (decision plane; execute/verify are harness-owned):
    understand → hydrate → qualify → effective capability →
    complete expected cost → STAY/SWITCH → choose strategy →
    (execute → verify → bounded recovery) → receipt

Chooses the cheapest **qualified complete** execution strategy, not the
cheapest next model call.

Frozen consume APIs
-------------------
* ``candidate_pool`` — ``CandidatePoolReceipt`` / ``ShortlistEntry`` (identity)
* ``effective_capability`` — ``AssistancePlan``, ``AssistanceCost``, ``TaskSlice``
* ``context_budget`` — ``BudgetReceipt``
* ``context_trust`` — eligibility deltas already applied upstream
* ``runtime_certification`` — ``CertificationState`` / freshness on offers
* ``expected_cost`` — ``ExpectedStrategyCost``, ``compare_strategies`` /
  ``select_strategy``, ``build_strategy_from_assistance``
* ``session_economics`` — ``ConcreteRoute``, ``SessionRouteDecision``
* ``bounded_recovery`` — ``RecoveryBounds`` (policy on receipt; controller
  remains authoritative for failure-time actions)
* ``eligibility`` / ``chooser`` — hard exclusions supplied as
  ``hard_excluded_ids``; never restored

Produced
--------
* ``ExecutionPathDecision`` — inspectable strategy receipt with selected /
  rejected strategies, concrete route identity, assistance digest, complete
  cost terms, budget/session/recovery evidence, digests, and why-selected.

Strategy vocabulary
-------------------
``direct_cheap`` | ``cheap_with_assistance`` |
``frontier_plan_then_cheap_execute`` | ``cheap_execute_then_verify`` |
``cheap_execute_then_rehydrate_retry`` | ``cheap_execute_then_bounded_escalate`` |
``stay_current_route`` | ``switch_equivalent_route`` | ``direct_paid`` |
``frontier_direct`` | ``blocked``
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from verdict.bounded_recovery import RecoveryBounds
from verdict.candidate_pool import CandidatePoolReceipt
from verdict.context_budget import BudgetReceipt
from verdict.effective_capability import AssistancePlan, TaskSlice
from verdict.expected_cost import (
    EXPECTED_COST_SCHEMA_VERSION,
    ExpectedStrategyCost,
    select_strategy,
)
from verdict.runtime_certification import CertificationState
from verdict.session_economics import ConcreteRoute, SessionRouteDecision

EXECUTION_PATH_SCHEMA_VERSION = "1"

StrategyName = Literal[
    "direct_cheap",
    "cheap_with_assistance",
    "frontier_plan_then_cheap_execute",
    "cheap_execute_then_verify",
    "cheap_execute_then_rehydrate_retry",
    "cheap_execute_then_bounded_escalate",
    "stay_current_route",
    "switch_equivalent_route",
    "direct_paid",
    "frontier_direct",
    "blocked",
]

STRATEGY_NAMES: frozenset[str] = frozenset(
    {
        "direct_cheap",
        "cheap_with_assistance",
        "frontier_plan_then_cheap_execute",
        "cheap_execute_then_verify",
        "cheap_execute_then_rehydrate_retry",
        "cheap_execute_then_bounded_escalate",
        "stay_current_route",
        "switch_equivalent_route",
        "direct_paid",
        "frontier_direct",
        "blocked",
    }
)

_UNHEALTHY_CERT = frozenset(
    {CertificationState.UNAVAILABLE, CertificationState.UNSUPPORTED, CertificationState.UNKNOWN}
)

# Sole strategy-selection authority for execution-path authority / legacy selector demotion. Legacy
# IntelligenceService.route / choose_route / live_routing.select_route /
# free-tier / AdaptiveRanker / FailoverEngine / Ruflo-swarm may feed evidence or
# dispatch only — they must not invent strategy outside optimize_execution_path.
# Production serve fails closed without an ExecutionPathDecision (see serve_path).
STRATEGY_AUTHORITY = "execution_path.optimize_execution_path"

_REQUIRED_COMPLETE_KINDS: frozenset[str] = frozenset({"execution", "verification"})


class ExecutionPathError(ValueError):
    """Raised when execution-path inputs violate the integrator contract."""


_STRATEGY_EXTRA_COST_KINDS: Mapping[str, frozenset[str]] = {
    "cheap_execute_then_rehydrate_retry": frozenset({"retry", "hydration"}),
    "cheap_execute_then_bounded_escalate": frozenset({"escalation"}),
    "switch_equivalent_route": frozenset({"route_switch"}),
    "cheap_with_assistance": frozenset({"hydration"}),
}


def _required_cost_kinds_for_offer(
    offer: ExecutionPathOffer, *, task_slice: TaskSlice, base_kinds: frozenset[str]
) -> frozenset[str]:
    """Require verification/recovery terms when the strategy/task needs them."""

    kinds = set(base_kinds)
    needs_verification = bool(task_slice.proof_criteria) or bool(
        offer.assistance_plan.verification.proof_criteria
    )
    if (
        offer.assistance_plan.verification.kind not in ("", "none")
        and offer.assistance_plan.verification.kind
    ):
        needs_verification = True
    if not needs_verification:
        kinds.discard("verification")
    extras = _STRATEGY_EXTRA_COST_KINDS.get(offer.strategy)
    if extras:
        # Accept any one of the strategy-specific kinds when multiple listed
        # (e.g. rehydrate may show as retry OR hydration).
        present = {term.kind for term in offer.expected_cost.terms}
        if not (extras & present):
            kinds.update(extras)
    return frozenset(kinds)


@dataclass(frozen=True)
class ExecutionPathOffer:
    """One complete-strategy alternative with precomputed evidence.

    Callers assemble offers from BOD-120/54/125/92 outputs. This module
    qualifies and ranks; it does not re-plan capability or re-price tokens.
    """

    strategy: StrategyName
    route: ConcreteRoute
    assistance_plan: AssistancePlan
    expected_cost: ExpectedStrategyCost
    budget_receipt: BudgetReceipt | None = None
    certification_state: CertificationState | None = None
    certification_freshness: str = "unknown"
    hard_excluded: bool = False
    is_cheap: bool = False
    is_paid: bool = False
    is_frontier: bool = False
    context_trust_admitted: bool = True
    evidence_conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGY_NAMES:
            raise ExecutionPathError(f"unknown strategy: {self.strategy!r}")
        if self.assistance_plan.candidate_id != self.route.route_id:
            raise ExecutionPathError(
                "assistance_plan.candidate_id must match route.route_id "
                f"({self.assistance_plan.candidate_id!r} != {self.route.route_id!r})"
            )
        if not self.expected_cost.strategy_id:
            raise ExecutionPathError("expected_cost.strategy_id must be non-empty")
        # Cost receipt must be bound to this concrete route (prevent foreign costs).
        sid = self.expected_cost.strategy_id
        if self.route.route_id not in sid and self.route.model not in sid:
            raise ExecutionPathError(
                "expected_cost.strategy_id must reference route_id or model "
                f"({sid!r} vs {self.route.route_id!r}/{self.route.model!r})"
            )
        object.__setattr__(self, "evidence_conflicts", tuple(self.evidence_conflicts))

    @property
    def candidate_id(self) -> str:
        return self.route.route_id


@dataclass(frozen=True)
class RejectedStrategy:
    """Named rejection for an evaluated strategy alternative."""

    strategy: StrategyName
    candidate_id: str | None
    reason: str
    expected_cost_strategy_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "candidate_id": self.candidate_id,
            "reason": self.reason,
            "expected_cost_strategy_id": self.expected_cost_strategy_id,
        }


@dataclass(frozen=True)
class ExecutionPathRequest:
    """Inputs for one deterministic execution-path decision."""

    task_slice: TaskSlice
    trajectory_id: str
    offers: Sequence[ExecutionPathOffer]
    session_decision: SessionRouteDecision | None = None
    recovery_bounds: RecoveryBounds | None = None
    pool_receipt: CandidatePoolReceipt | None = None
    hard_excluded_ids: frozenset[str] = field(default_factory=frozenset)
    now: datetime | None = None
    assumptions: tuple[str, ...] = ()
    allow_degraded_certification: bool = False
    require_complete_cost_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(_REQUIRED_COMPLETE_KINDS)
    )
    prequalified_stronger_route_ids: frozenset[str] = field(default_factory=frozenset)
    evidence_conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.trajectory_id or not str(self.trajectory_id).strip():
            raise ExecutionPathError("trajectory_id must be non-empty")
        object.__setattr__(self, "offers", tuple(self.offers))
        object.__setattr__(self, "hard_excluded_ids", frozenset(self.hard_excluded_ids))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        object.__setattr__(
            self, "require_complete_cost_kinds", frozenset(self.require_complete_cost_kinds)
        )
        object.__setattr__(
            self, "prequalified_stronger_route_ids", frozenset(self.prequalified_stronger_route_ids)
        )
        object.__setattr__(self, "evidence_conflicts", tuple(self.evidence_conflicts))
        if self.now is not None and self.now.tzinfo is None:
            raise ExecutionPathError("now must be timezone-aware when provided")


@dataclass(frozen=True)
class ExecutionPathDecision:
    """Inspectable receipt for the selected complete execution path."""

    selected_strategy: StrategyName
    selected_candidate_id: str | None
    selected_route: ConcreteRoute | None
    rejected: tuple[RejectedStrategy, ...]
    assistance_plan_id: str | None
    assistance_plan_digest: str | None
    expected_cost: ExpectedStrategyCost | None
    expected_cost_terms: tuple[dict[str, Any], ...]
    budget_state: Mapping[str, Any] | None
    tools_surface: tuple[str, ...]
    session_decision: Mapping[str, Any] | None
    recovery_policy: Mapping[str, Any] | None
    verification_requirements: tuple[str, ...]
    assumptions: tuple[str, ...]
    unknowns: tuple[str, ...]
    freshness: Mapping[str, Any]
    evidence_digests: Mapping[str, str]
    why_selected: str
    decision_digest: str
    trajectory_id: str
    task_slice_id: str
    strategy_selection_reason: str | None = None
    schema_version: str = EXECUTION_PATH_SCHEMA_VERSION
    expected_cost_schema_version: str = EXPECTED_COST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "expected_cost_schema_version": self.expected_cost_schema_version,
            "trajectory_id": self.trajectory_id,
            "task_slice_id": self.task_slice_id,
            "selected_strategy": self.selected_strategy,
            "selected_candidate_id": self.selected_candidate_id,
            "selected_route": None
            if self.selected_route is None
            else self.selected_route.to_dict(),
            "rejected": [item.to_dict() for item in self.rejected],
            "assistance_plan_id": self.assistance_plan_id,
            "assistance_plan_digest": self.assistance_plan_digest,
            "expected_cost": None if self.expected_cost is None else self.expected_cost.to_dict(),
            "expected_cost_terms": list(self.expected_cost_terms),
            "budget_state": None if self.budget_state is None else dict(self.budget_state),
            "tools_surface": list(self.tools_surface),
            "session_decision": None
            if self.session_decision is None
            else dict(self.session_decision),
            "recovery_policy": None if self.recovery_policy is None else dict(self.recovery_policy),
            "verification_requirements": list(self.verification_requirements),
            "assumptions": list(self.assumptions),
            "unknowns": list(self.unknowns),
            "freshness": dict(self.freshness),
            "evidence_digests": dict(self.evidence_digests),
            "why_selected": self.why_selected,
            "strategy_selection_reason": self.strategy_selection_reason,
            "decision_digest": self.decision_digest,
        }


def _format_datetime(value: datetime) -> str:
    # Preserve sub-second precision when present. Recovery authority compares
    # optimizer receipts against an exact re-plan boundary, so truncation would
    # make a same-second cached decision indistinguishable from a fresh one.
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _recovery_policy_dict(bounds: RecoveryBounds | None) -> dict[str, Any] | None:
    if bounds is None:
        return None
    return {
        "max_attempts": bounds.max_attempts,
        "deadline_at": None if bounds.deadline_at is None else _format_datetime(bounds.deadline_at),
        "estimated_action_cash_usd": str(bounds.estimated_action_cash_usd),
        "estimated_action_tokens": str(bounds.estimated_action_tokens),
        "schema": "bounded_recovery/RecoveryBounds",
        "note": "failure-time actions owned by BoundedRecoveryController; not success by policy",
    }


def _qualify_offer(
    offer: ExecutionPathOffer,
    *,
    task_slice: TaskSlice,
    trajectory_id: str,
    hard_excluded_ids: frozenset[str],
    allow_degraded_certification: bool,
    require_complete_cost_kinds: frozenset[str],
) -> str | None:
    """Return a rejection reason, or None if the offer is qualified."""

    cid = offer.candidate_id
    plan_slice = offer.assistance_plan.task_slice
    if plan_slice.slice_id != task_slice.slice_id:
        return "evidence_unbound_task_slice_id"
    if plan_slice.objective != task_slice.objective:
        return "evidence_unbound_task_objective"
    if plan_slice.proof_criteria != task_slice.proof_criteria:
        return "evidence_unbound_proof_criteria"
    if plan_slice.acceptance_criteria != task_slice.acceptance_criteria:
        return "evidence_unbound_acceptance_criteria"
    if offer.expected_cost.trajectory_id != trajectory_id:
        return "evidence_unbound_trajectory_id"
    if offer.budget_receipt is not None and offer.budget_receipt.candidate_id != cid:
        return "evidence_unbound_budget_candidate"
    if offer.hard_excluded or cid in hard_excluded_ids or offer.route.excluded:
        return "hard_excluded_never_restored"
    if offer.route.hard_ineligible or not offer.route.eligible:
        return "hard_ineligible_route"
    if not offer.context_trust_admitted:
        return "context_trust_not_admitted"
    if offer.evidence_conflicts:
        return f"evidence_conflict:{','.join(offer.evidence_conflicts)}"
    if offer.assistance_plan.result == "unknown":
        return "unknown_capability_never_sufficient"
    if offer.assistance_plan.result != "sufficient":
        reasons = ",".join(offer.assistance_plan.reasons) or "insufficient"
        if any("tool" in r for r in offer.assistance_plan.reasons):
            return f"insufficient_missing_tool:{reasons}"
        return f"insufficient:{reasons}"
    if (
        not offer.assistance_plan.assisted_sufficient
        and not offer.assistance_plan.intrinsic_sufficient
    ):
        return "not_assisted_sufficient"
    if offer.budget_receipt is not None:
        if not offer.budget_receipt.fits:
            return "budget_dropped_mandatory_context"
        if any(om.priority == "mandatory" for om in offer.budget_receipt.omitted):
            return "budget_omitted_mandatory_context"
    freshness = (offer.certification_freshness or "unknown").lower()
    if freshness in {"stale", "unknown", ""}:
        return (
            "stale_runtime_certification"
            if freshness == "stale"
            else "unknown_certification_freshness_not_optimistic"
        )
    if offer.certification_state in _UNHEALTHY_CERT:
        return f"certification_{offer.certification_state.value}_not_optimistic"
    if (
        offer.certification_state is CertificationState.DEGRADED
        and not allow_degraded_certification
    ):
        return "degraded_certification_requires_explicit_policy"
    if offer.certification_state is None:
        return "certification_required_for_qualify"
    if offer.expected_cost.has_unknown_cash:
        return "unknown_price_not_treated_as_free"
    if not offer.expected_cost.qualified:
        return "expected_cost_not_qualified"
    required_kinds = _required_cost_kinds_for_offer(
        offer, task_slice=task_slice, base_kinds=require_complete_cost_kinds
    )
    present_kinds = {term.kind for term in offer.expected_cost.terms}
    missing = sorted(required_kinds - present_kinds)
    if missing:
        return f"incomplete_expected_cost_missing:{','.join(missing)}"
    # Opaque auto/* identities never satisfy execution evidence.
    for field_name in ("route_id", "gateway", "provider", "model"):
        value = getattr(offer.route, field_name)
        if isinstance(value, str) and (value.startswith("auto/") or value == "auto"):
            return f"opaque_auto_identity_rejected:{field_name}"
    return None


def optimize_execution_path(request: ExecutionPathRequest) -> ExecutionPathDecision:
    """Qualify offers, rank complete expected costs, emit an inspectable receipt."""

    when = request.now or datetime.now(timezone.utc)
    rejected: list[RejectedStrategy] = []
    qualified: list[ExecutionPathOffer] = []
    unknowns: list[str] = []
    assumptions = list(request.assumptions)
    assumptions.append(f"strategy_authority={STRATEGY_AUTHORITY}")

    # Request-level conflicts fail closed — never rank while conflicts remain.
    if request.evidence_conflicts:
        for conflict in request.evidence_conflicts:
            rejected.append(
                RejectedStrategy(
                    strategy="blocked", candidate_id=None, reason=f"evidence_conflict:{conflict}"
                )
            )
            unknowns.append(f"conflict:{conflict}")
        conflict_payload: dict[str, Any] = {
            "trajectory_id": request.trajectory_id,
            "selected_strategy": "blocked",
            "conflicts": list(request.evidence_conflicts),
            "when": _format_datetime(when),
        }
        return ExecutionPathDecision(
            selected_strategy="blocked",
            selected_candidate_id=None,
            selected_route=None,
            rejected=tuple(rejected),
            assistance_plan_id=None,
            assistance_plan_digest=None,
            expected_cost=None,
            expected_cost_terms=(),
            budget_state=None,
            tools_surface=(),
            session_decision=None
            if request.session_decision is None
            else request.session_decision.to_dict(),
            recovery_policy=_recovery_policy_dict(request.recovery_bounds),
            verification_requirements=tuple(request.task_slice.proof_criteria),
            assumptions=tuple(
                dict.fromkeys([*assumptions, "request_evidence_conflicts_fail_closed"])
            ),
            unknowns=tuple(dict.fromkeys(unknowns)),
            freshness={"decision_at": _format_datetime(when)},
            evidence_digests={},
            why_selected="blocked_evidence_conflicts",
            decision_digest=_digest(conflict_payload),
            trajectory_id=request.trajectory_id,
            task_slice_id=request.task_slice.slice_id,
            strategy_selection_reason="evidence_conflicts",
        )

    hard_ids = set(request.hard_excluded_ids)
    shortlist_ids: frozenset[str] | None = None
    if request.pool_receipt is not None:
        for drop in request.pool_receipt.hard_drops:
            hard_ids.add(drop.route_id)
        shortlist_ids = frozenset(entry.route_id for entry in request.pool_receipt.shortlist)

    # Authoritative session BLOCKED vetoes all dispatch.
    if (
        request.session_decision is not None
        and request.session_decision.authoritative
        and request.session_decision.decision == "BLOCKED"
    ):
        rejected.append(
            RejectedStrategy(
                strategy="blocked",
                candidate_id=request.session_decision.selected_route_id,
                reason="authoritative_session_blocked",
            )
        )
        blocked_session_payload: dict[str, Any] = {
            "trajectory_id": request.trajectory_id,
            "selected_strategy": "blocked",
            "reason": "authoritative_session_blocked",
            "when": _format_datetime(when),
        }
        return ExecutionPathDecision(
            selected_strategy="blocked",
            selected_candidate_id=None,
            selected_route=None,
            rejected=tuple(rejected),
            assistance_plan_id=None,
            assistance_plan_digest=None,
            expected_cost=None,
            expected_cost_terms=(),
            budget_state=None,
            tools_surface=(),
            session_decision=request.session_decision.to_dict(),
            recovery_policy=_recovery_policy_dict(request.recovery_bounds),
            verification_requirements=tuple(request.task_slice.proof_criteria),
            assumptions=tuple(dict.fromkeys([*assumptions, "authoritative_session_blocked"])),
            unknowns=tuple(dict.fromkeys(unknowns)),
            freshness={"decision_at": _format_datetime(when)},
            evidence_digests={},
            why_selected="blocked_authoritative_session",
            decision_digest=_digest(blocked_session_payload),
            trajectory_id=request.trajectory_id,
            task_slice_id=request.task_slice.slice_id,
            strategy_selection_reason="authoritative_session_blocked",
        )

    # Session preference cannot restore hard-excluded or EC-incapable candidates.
    session_pref: str | None = None
    if request.session_decision is not None:
        session_pref = request.session_decision.selected_route_id
        if session_pref is not None and session_pref in hard_ids:
            assumptions.append("session_preference_ignored_hard_excluded")
            rejected.append(
                RejectedStrategy(
                    strategy="switch_equivalent_route",
                    candidate_id=session_pref,
                    reason="hard_excluded_never_restored_by_session",
                )
            )
            session_pref = None

    pool_drop_reasons = {
        drop.route_id: drop.reason
        for drop in (request.pool_receipt.hard_drops if request.pool_receipt is not None else ())
        if drop.reason
    }
    offered_candidate_ids = {offer.candidate_id for offer in request.offers}
    for candidate_id, pool_drop_reason in pool_drop_reasons.items():
        if candidate_id not in offered_candidate_ids:
            rejected.append(
                RejectedStrategy(
                    strategy="blocked", candidate_id=candidate_id, reason=pool_drop_reason
                )
            )
    for offer in request.offers:
        if shortlist_ids is not None and offer.candidate_id not in shortlist_ids:
            drop_reason = pool_drop_reasons.get(offer.candidate_id)
            rejection_reason = drop_reason if drop_reason else "not_in_candidate_pool_shortlist"
            rejected.append(
                RejectedStrategy(
                    strategy=offer.strategy,
                    candidate_id=offer.candidate_id,
                    reason=rejection_reason,
                    expected_cost_strategy_id=offer.expected_cost.strategy_id,
                )
            )
            continue
        reason = _qualify_offer(
            offer,
            task_slice=request.task_slice,
            trajectory_id=request.trajectory_id,
            hard_excluded_ids=frozenset(hard_ids),
            allow_degraded_certification=request.allow_degraded_certification,
            require_complete_cost_kinds=request.require_complete_cost_kinds,
        )
        if reason is not None:
            if "unknown" in reason:
                unknowns.append(f"{offer.candidate_id}:{reason}")
            rejected.append(
                RejectedStrategy(
                    strategy=offer.strategy,
                    candidate_id=offer.candidate_id,
                    reason=reason,
                    expected_cost_strategy_id=offer.expected_cost.strategy_id,
                )
            )
            continue
        qualified.append(offer)

    # P0: never STAY on an EC-incapable current route.
    if (
        request.session_decision is not None
        and request.session_decision.decision == "STAY"
        and session_pref is not None
    ):
        capable_ids = {offer.candidate_id for offer in qualified}
        if session_pref not in capable_ids:
            assumptions.append("session_stay_overridden_effective_capability_insufficient")
            rejected.append(
                RejectedStrategy(
                    strategy="stay_current_route",
                    candidate_id=session_pref,
                    reason="stay_blocked_current_not_effective_capable",
                )
            )
            session_pref = None

    if not qualified:
        blocked_payload: dict[str, Any] = {
            "trajectory_id": request.trajectory_id,
            "task_slice_id": request.task_slice.slice_id,
            "selected_strategy": "blocked",
            "rejected": [item.to_dict() for item in rejected],
            "when": _format_datetime(when),
        }
        return ExecutionPathDecision(
            selected_strategy="blocked",
            selected_candidate_id=None,
            selected_route=None,
            rejected=tuple(rejected),
            assistance_plan_id=None,
            assistance_plan_digest=None,
            expected_cost=None,
            expected_cost_terms=(),
            budget_state=None,
            tools_surface=(),
            session_decision=None
            if request.session_decision is None
            else request.session_decision.to_dict(),
            recovery_policy=_recovery_policy_dict(request.recovery_bounds),
            verification_requirements=tuple(request.task_slice.proof_criteria),
            assumptions=tuple(assumptions),
            unknowns=tuple(dict.fromkeys(unknowns)),
            freshness={"decision_at": _format_datetime(when)},
            evidence_digests={},
            why_selected="no_qualified_complete_strategies",
            decision_digest=_digest(blocked_payload),
            trajectory_id=request.trajectory_id,
            task_slice_id=request.task_slice.slice_id,
            strategy_selection_reason="no_qualified_strategies",
        )

    # Authoritative STAY/SWITCH constrains ranking to the session-selected route
    # when that route remains qualified (preserves hysteresis / hard overrides).
    rank_pool = list(qualified)
    if (
        request.session_decision is not None
        and request.session_decision.authoritative
        and session_pref is not None
    ):
        session_offers = [offer for offer in qualified if offer.candidate_id == session_pref]
        if session_offers:
            assumptions.append("authoritative_session_constrains_selection")
            for offer in qualified:
                if offer.candidate_id == session_pref:
                    continue
                rejected.append(
                    RejectedStrategy(
                        strategy=offer.strategy,
                        candidate_id=offer.candidate_id,
                        reason="deferred_to_authoritative_session_route",
                        expected_cost_strategy_id=offer.expected_cost.strategy_id,
                    )
                )
            rank_pool = session_offers

    # Duplicate strategy_id among the rank pool would map selection ambiguously.
    seen_ids: dict[str, str] = {}
    for offer in rank_pool:
        sid = offer.expected_cost.strategy_id
        if sid in seen_ids:
            raise ExecutionPathError(
                f"duplicate expected_cost.strategy_id {sid!r} among qualified offers "
                f"({seen_ids[sid]!r} and {offer.candidate_id!r})"
            )
        seen_ids[sid] = offer.candidate_id

    selection = select_strategy(
        [offer.expected_cost for offer in rank_pool], mode="expected_cost", free_first=False
    )

    selected_offer: ExecutionPathOffer | None = None
    if selection.selected_strategy_id is not None:
        for offer in rank_pool:
            if offer.expected_cost.strategy_id == selection.selected_strategy_id:
                selected_offer = offer
                break

    # If economics returned None (unknown cash among all), fall through to blocked.
    if selected_offer is None:
        for offer in rank_pool:
            rejected.append(
                RejectedStrategy(
                    strategy=offer.strategy,
                    candidate_id=offer.candidate_id,
                    reason="unknown_or_unranked_complete_cost",
                    expected_cost_strategy_id=offer.expected_cost.strategy_id,
                )
            )
        unranked_payload: dict[str, Any] = {
            "trajectory_id": request.trajectory_id,
            "selected_strategy": "blocked",
            "reason": selection.reason,
            "when": _format_datetime(when),
        }
        return ExecutionPathDecision(
            selected_strategy="blocked",
            selected_candidate_id=None,
            selected_route=None,
            rejected=tuple(rejected),
            assistance_plan_id=None,
            assistance_plan_digest=None,
            expected_cost=None,
            expected_cost_terms=(),
            budget_state=None,
            tools_surface=(),
            session_decision=None
            if request.session_decision is None
            else request.session_decision.to_dict(),
            recovery_policy=_recovery_policy_dict(request.recovery_bounds),
            verification_requirements=tuple(request.task_slice.proof_criteria),
            assumptions=tuple(assumptions),
            unknowns=tuple(dict.fromkeys([*unknowns, "complete_cost_unranked"])),
            freshness={"decision_at": _format_datetime(when)},
            evidence_digests={},
            why_selected=f"blocked:{selection.reason}",
            decision_digest=_digest(unranked_payload),
            trajectory_id=request.trajectory_id,
            task_slice_id=request.task_slice.slice_id,
            strategy_selection_reason=selection.reason,
        )

    # Prefer explicit session STAY/SWITCH strategy label when that route won.
    selected_strategy: StrategyName = selected_offer.strategy
    if (
        request.session_decision is not None
        and session_pref is not None
        and selected_offer.candidate_id == session_pref
    ):
        if request.session_decision.decision == "STAY":
            selected_strategy = "stay_current_route"
        elif request.session_decision.decision == "SWITCH":
            selected_strategy = "switch_equivalent_route"

    # Record cost-based rejections for other ranked offers.
    for offer in rank_pool:
        if offer is selected_offer:
            continue
        cash_note = ""
        if (
            selected_offer.expected_cost.cash_usd is not None
            and offer.expected_cost.cash_usd is not None
        ):
            cash_note = (
                f"; selected_cash={selected_offer.expected_cost.cash_usd} "
                f"< offer_cash={offer.expected_cost.cash_usd}"
            )
        rejected.append(
            RejectedStrategy(
                strategy=offer.strategy,
                candidate_id=offer.candidate_id,
                reason=f"higher_complete_expected_cost{cash_note}",
                expected_cost_strategy_id=offer.expected_cost.strategy_id,
            )
        )

    plan = selected_offer.assistance_plan
    verification = tuple(plan.verification.proof_criteria) or tuple(
        request.task_slice.proof_criteria
    )
    # Verification requirements survive decomposition.
    if plan.decomposition.required and not plan.decomposition.preserves_parent_proof:
        assumptions.append("decomposition_must_preserve_parent_proof")
    verification = tuple(dict.fromkeys((*verification, *request.task_slice.proof_criteria)))

    budget_state = (
        None if selected_offer.budget_receipt is None else selected_offer.budget_receipt.to_dict()
    )
    digests: dict[str, str] = {
        "assistance_plan": plan.evidence_digest,
        "expected_cost_strategy": selected_offer.expected_cost.strategy_id,
    }
    if selected_offer.budget_receipt is not None:
        digests["budget"] = selected_offer.budget_receipt.digest
    if request.pool_receipt is not None:
        digests["candidate_pool"] = request.pool_receipt.evidence_digest

    freshness: dict[str, Any] = {
        "decision_at": _format_datetime(when),
        "certification_freshness": selected_offer.certification_freshness,
        "certification_state": None
        if selected_offer.certification_state is None
        else selected_offer.certification_state.value,
    }
    if request.session_decision is not None:
        freshness["session"] = dict(request.session_decision.freshness)

    why = (
        f"selected_{selected_strategy}_via_{selection.reason}; "
        f"complete_expected_cost beats {len(rank_pool) - 1} ranked alternative(s)"
    )
    if selected_offer.expected_cost.cash_usd is not None:
        why += f"; cash_usd={selected_offer.expected_cost.cash_usd}"

    receipt_body = {
        "trajectory_id": request.trajectory_id,
        "task_slice_id": request.task_slice.slice_id,
        "selected_strategy": selected_strategy,
        "selected_candidate_id": selected_offer.candidate_id,
        "route": selected_offer.route.to_dict(),
        "assistance_plan_digest": plan.evidence_digest,
        "expected_cost": selected_offer.expected_cost.to_dict(),
        "rejected": [item.to_dict() for item in rejected],
        "selection_reason": selection.reason,
        "when": _format_datetime(when),
    }
    decision_digest = _digest(receipt_body)

    return ExecutionPathDecision(
        selected_strategy=selected_strategy,
        selected_candidate_id=selected_offer.candidate_id,
        selected_route=selected_offer.route,
        rejected=tuple(rejected),
        assistance_plan_id=plan.plan_id,
        assistance_plan_digest=plan.evidence_digest,
        expected_cost=selected_offer.expected_cost,
        expected_cost_terms=tuple(term.to_dict() for term in selected_offer.expected_cost.terms),
        budget_state=budget_state,
        tools_surface=tuple(plan.selected_tool_surface),
        session_decision=None
        if request.session_decision is None
        else request.session_decision.to_dict(),
        recovery_policy=_recovery_policy_dict(request.recovery_bounds),
        verification_requirements=verification,
        assumptions=tuple(dict.fromkeys(assumptions)),
        unknowns=tuple(dict.fromkeys(unknowns)),
        freshness=freshness,
        evidence_digests=digests,
        why_selected=why,
        decision_digest=decision_digest,
        trajectory_id=request.trajectory_id,
        task_slice_id=request.task_slice.slice_id,
        strategy_selection_reason=selection.reason,
    )


def _stronger_route_cert_ready(item: Any, certification: Any) -> bool:
    """READY gate for escalate targets (parity with equivalent-plane switch)."""

    from verdict.runtime_certification import ComponentKind, RuntimeCertificationReport

    if not isinstance(certification, RuntimeCertificationReport):
        # No certification evidence → cannot optimistically escalate.
        return False
    if item.gateway_id:
        ready = False
        for component in certification.components:
            if (
                component.kind is ComponentKind.GATEWAY
                and (
                    component.component_id == item.gateway_id
                    or component.identity == item.gateway_id
                )
                and component.state is CertificationState.READY
            ):
                ready = True
                break
        if not ready:
            return False
    provider_listed = any(
        c.component_id == item.provider or c.identity == item.provider
        for c in certification.components
    )
    if provider_listed:
        ready = False
        for component in certification.components:
            if (
                component.kind in {ComponentKind.PROVIDER, ComponentKind.GATEWAY}
                and (component.component_id == item.provider or component.identity == item.provider)
                and component.state is CertificationState.READY
            ):
                ready = True
                break
        if not ready:
            return False
    return True


def apply_bounded_recovery(
    *,
    controller: Any,
    evidence: Any,
    route: Any,
    ledger: Any,
    certification: Any = None,
    equivalent_routes: Sequence[Any] = (),
    stronger_routes: Sequence[Any] = (),
    prequalified_stronger_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
    reserve_cash_usd: Any = None,
) -> Any:
    """Delegate failure recovery to :class:`BoundedRecoveryController`.

    Escalation candidates are filtered to BOD-104-prequalified stronger routes
    that also pass READY certification (same gate as equivalent switch).
    Reserves against the recovery cash envelope before deciding.
    This does not invent success; cancellation/ambiguous outcomes stay non-success.
    """

    from decimal import Decimal

    from verdict.bounded_recovery import BoundedRecoveryController, ExecutionRoute
    from verdict.cost_ledger import CostLedger, CostLedgerError

    if not isinstance(controller, BoundedRecoveryController):
        raise ExecutionPathError("controller must be a BoundedRecoveryController")
    if not isinstance(ledger, CostLedger):
        raise ExecutionPathError("ledger must be a CostLedger")

    filtered_stronger: list[ExecutionRoute] = []
    for item in stronger_routes:
        if not isinstance(item, ExecutionRoute):
            continue
        # Empty prequalification means no approved escalation targets (fail-closed).
        if item.model_id not in prequalified_stronger_ids:
            continue
        if not _stronger_route_cert_ready(item, certification):
            continue
        filtered_stronger.append(item)

    cash_amount = (
        Decimal(str(reserve_cash_usd))
        if reserve_cash_usd is not None
        else controller.bounds.estimated_action_cash_usd
    )
    try:
        ledger.reserve(cash_amount, pool="cash", unit="usd", now=now)
    except CostLedgerError as exc:
        raise ExecutionPathError(f"recovery_reserve_failed:{exc}") from exc

    return controller.decide(
        evidence=evidence,
        route=route,
        ledger=ledger,
        certification=certification,
        equivalent_routes=equivalent_routes,
        stronger_routes=tuple(filtered_stronger),
        now=now,
    )


def legacy_selector_must_yield(
    *,
    execution_path_decision: ExecutionPathDecision | Mapping[str, Any],
    legacy_selected_model_id: str | None,
) -> None:
    """Fail closed when a legacy selector disagrees with execution-path authority.

    Used by thin serve-path hooks so ``IntelligenceService.route`` / chooser
    cannot outrank an already-computed :class:`ExecutionPathDecision`.
    """

    selected: str | None
    strategy: str
    route: ConcreteRoute | None
    if isinstance(execution_path_decision, ExecutionPathDecision):
        selected = execution_path_decision.selected_candidate_id
        strategy = str(execution_path_decision.selected_strategy)
        route = execution_path_decision.selected_route
    else:
        raw_selected = execution_path_decision.get("selected_candidate_id")
        selected = raw_selected if isinstance(raw_selected, str) else None
        strategy = str(execution_path_decision.get("selected_strategy") or "")
        route_raw = execution_path_decision.get("selected_route")
        route = None
        if isinstance(route_raw, Mapping):
            fallback = route_raw.get("route_id") or route_raw.get("model")
            if selected is None and isinstance(fallback, str):
                selected = fallback

    if strategy == "blocked" or selected is None:
        reasons: list[str] = []
        raw_rejected = (
            execution_path_decision.rejected
            if isinstance(execution_path_decision, ExecutionPathDecision)
            else execution_path_decision.get("rejected")
        )
        if isinstance(raw_rejected, tuple | list):
            for item in raw_rejected:
                reason = item.reason if isinstance(item, RejectedStrategy) else None
                if reason is None and isinstance(item, Mapping):
                    raw_reason = item.get("reason")
                    reason = raw_reason if isinstance(raw_reason, str) else None
                if isinstance(reason, str) and reason.strip() and reason not in reasons:
                    reasons.append(reason)
        why = (
            execution_path_decision.why_selected
            if isinstance(execution_path_decision, ExecutionPathDecision)
            else execution_path_decision.get("why_selected")
        )
        why_text = why.strip() if isinstance(why, str) else ""
        detail = f" reasons={','.join(reasons)}" if reasons else ""
        why_detail = f" why={why_text}" if why_text else ""
        raise ExecutionPathError(
            "BOD-104 strategy authority blocked dispatch; legacy selector must not invent a route"
            f"{why_detail}{detail}"
        )
    if legacy_selected_model_id is None:
        return
    allowed = {selected}
    if route is not None and isinstance(route, ConcreteRoute):
        allowed.update({route.route_id, route.model})
    elif isinstance(execution_path_decision, Mapping):
        route_map = execution_path_decision.get("selected_route")
        if isinstance(route_map, Mapping):
            for key in ("route_id", "model"):
                value = route_map.get(key)
                if isinstance(value, str):
                    allowed.add(value)
    if legacy_selected_model_id not in allowed:
        raise ExecutionPathError(
            f"legacy selector {legacy_selected_model_id!r} conflicts with BOD-104 "
            f"authority selected={sorted(allowed)!r} ({STRATEGY_AUTHORITY})"
        )


def infer_strategy_name(
    *,
    plan: AssistancePlan,
    is_cheap: bool = False,
    is_paid: bool = False,
    is_frontier: bool = False,
    session_decision: str | None = None,
    prefer_verify: bool = False,
    prefer_rehydrate: bool = False,
    prefer_escalate: bool = False,
) -> StrategyName:
    """Map assistance/session flags to the execution-path authority strategy vocabulary.

    Helper for callers assembling :class:`ExecutionPathOffer` rows. Does not
    qualify or price — only names the strategy axis.
    """

    if session_decision == "STAY":
        return "stay_current_route"
    if session_decision == "SWITCH":
        return "switch_equivalent_route"
    if plan.result != "sufficient":
        return "blocked"
    if prefer_rehydrate and is_cheap:
        return "cheap_execute_then_rehydrate_retry"
    if prefer_escalate and is_cheap:
        return "cheap_execute_then_bounded_escalate"
    if plan.decomposition.required and is_cheap:
        return "frontier_plan_then_cheap_execute"
    if prefer_verify and is_cheap:
        return "cheap_execute_then_verify"
    if is_frontier and plan.intrinsic_sufficient:
        return "frontier_direct"
    if is_paid and plan.intrinsic_sufficient:
        return "direct_paid"
    if is_cheap and plan.intrinsic_sufficient:
        return "direct_cheap"
    if is_cheap and plan.assisted_sufficient:
        return "cheap_with_assistance"
    if is_frontier:
        return "frontier_direct"
    if is_paid:
        return "direct_paid"
    if is_cheap:
        return "cheap_with_assistance"
    return "blocked"


__all__ = [
    "EXECUTION_PATH_SCHEMA_VERSION",
    "STRATEGY_AUTHORITY",
    "STRATEGY_NAMES",
    "ExecutionPathDecision",
    "ExecutionPathError",
    "ExecutionPathOffer",
    "ExecutionPathRequest",
    "RejectedStrategy",
    "StrategyName",
    "apply_bounded_recovery",
    "infer_strategy_name",
    "legacy_selector_must_yield",
    "optimize_execution_path",
]
