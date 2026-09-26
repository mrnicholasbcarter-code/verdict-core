"""Failure-directed bounded recovery (bounded recovery).

Classify observable failures and choose the cheapest safe corrective action
within attempt, deadline, and cost-ledger budgets. This module is the recovery
policy layer — it does **not** overload ``verdict.escalation`` (keyword-tier
scanner) and does **not** implement BOD-104 planning or BOD-119 session
economics.

Preference order for corrective actions:
rehydrate → retry → repair tool/environment → equivalent execution-plane switch
→ replan → capability escalation → block.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from verdict.cost_ledger import CostLedger
from verdict.execution_session import FailureEntry
from verdict.runtime_certification import (
    CertificationState,
    ComponentKind,
    RuntimeCertificationReport,
)

BOUNDED_RECOVERY_SCHEMA_VERSION = "1"


class RecoveryFailureClass(str, Enum):
    """Observable failure classes for recovery policy."""

    MISSING_OR_STALE_CONTEXT = "missing_or_stale_context"
    PROVIDER_OR_GATEWAY_FAILURE = "provider_or_gateway_failure"
    MODEL_CAPABILITY_DEFICIT = "model_capability_deficit"
    TOOL_OR_ENVIRONMENT_MISMATCH = "tool_or_environment_mismatch"
    BAD_DECOMPOSITION_OR_PLAN = "bad_decomposition_or_plan"
    IMPLEMENTATION_ERROR = "implementation_error"
    VERIFICATION_OR_TEST_INFRA_FAILURE = "verification_or_test_infra_failure"
    AMBIGUOUS_OR_UNKNOWN = "ambiguous_or_unknown"


class RecoveryAction(str, Enum):
    """Cheapest-safe corrective actions (ordered by preference in policy)."""

    REHYDRATE = "rehydrate"
    RETRY = "retry"
    REPAIR_TOOL_OR_ENVIRONMENT = "repair_tool_or_environment"
    SWITCH_EXECUTION_PLANE = "switch_execution_plane"
    REPLAN = "replan"
    ESCALATE_CAPABILITY = "escalate_capability"
    BLOCK = "block"


class RecoveryOutcome(str, Enum):
    """Terminal/continuing outcomes. SUCCESS is never emitted for ambiguous/cancel."""

    CONTINUE = "continue"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUCCESS = "success"


@dataclass(frozen=True)
class ExecutionRoute:
    """Lightweight route snapshot — not a second OmniRoute abstraction."""

    model_id: str
    provider: str
    gateway_id: str | None = None
    capability_tier: int = 0
    is_frontier: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "gateway_id": self.gateway_id,
            "capability_tier": self.capability_tier,
            "is_frontier": self.is_frontier,
        }


@dataclass(frozen=True)
class FailureEvidence:
    """Deterministic evidence used for classification and replay."""

    error_class: str = ""
    message: str = ""
    status_code: int | None = None
    signals: frozenset[str] = frozenset()
    side_effects_ambiguous: bool = False
    cancelled: bool = False
    generating_worker_id: str | None = None
    verification_worker_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.signals, frozenset):
            object.__setattr__(self, "signals", frozenset(self.signals))

    def evidence_digest(self) -> str:
        payload = {
            "error_class": self.error_class,
            "message": self.message,
            "status_code": self.status_code,
            "signals": sorted(self.signals),
            "side_effects_ambiguous": self.side_effects_ambiguous,
            "cancelled": self.cancelled,
            "generating_worker_id": self.generating_worker_id,
            "verification_worker_id": self.verification_worker_id,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def from_failure_entry(
        cls,
        entry: FailureEntry,
        *,
        signals: frozenset[str] = frozenset(),
        generating_worker_id: str | None = None,
        verification_worker_id: str | None = None,
        cancelled: bool = False,
        side_effects_ambiguous: bool = False,
    ) -> FailureEvidence:
        return cls(
            error_class=entry.error_class,
            message=entry.message,
            status_code=entry.status_code,
            signals=signals,
            side_effects_ambiguous=side_effects_ambiguous,
            cancelled=cancelled,
            generating_worker_id=generating_worker_id,
            verification_worker_id=verification_worker_id,
        )


@dataclass(frozen=True)
class RecoveryBounds:
    """Hard bounds for recovery attempts."""

    max_attempts: int = 3
    deadline_at: datetime | None = None
    estimated_action_cash_usd: Decimal = field(default_factory=lambda: Decimal("0.01"))
    estimated_action_tokens: Decimal = field(default_factory=lambda: Decimal("100"))

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        object.__setattr__(
            self, "estimated_action_cash_usd", Decimal(str(self.estimated_action_cash_usd))
        )
        object.__setattr__(
            self, "estimated_action_tokens", Decimal(str(self.estimated_action_tokens))
        )


@dataclass(frozen=True)
class RecoveryDecision:
    """Explainable, replayable recovery decision."""

    failure_class: RecoveryFailureClass
    action: RecoveryAction
    outcome: RecoveryOutcome
    route_before: ExecutionRoute
    route_after: ExecutionRoute
    reason: str
    attempt_index: int
    evidence_digest: str
    created_at: datetime
    escalated: bool = False
    frontier_call: bool = False
    replan_signal: bool = False
    charge_generating_model: bool = True
    success: bool = False
    budget_cash_remaining_usd: str | None = None
    schema_version: str = BOUNDED_RECOVERY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "failure_class": self.failure_class.value,
            "action": self.action.value,
            "outcome": self.outcome.value,
            "route_before": self.route_before.to_dict(),
            "route_after": self.route_after.to_dict(),
            "reason": self.reason,
            "attempt_index": self.attempt_index,
            "evidence_digest": self.evidence_digest,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "escalated": self.escalated,
            "frontier_call": self.frontier_call,
            "replan_signal": self.replan_signal,
            "charge_generating_model": self.charge_generating_model,
            "success": self.success,
            "budget_cash_remaining_usd": self.budget_cash_remaining_usd,
        }


_SIGNAL_CLASS: tuple[tuple[frozenset[str], RecoveryFailureClass], ...] = (
    (
        frozenset({"missing_context", "stale_context", "context_missing", "context_stale"}),
        RecoveryFailureClass.MISSING_OR_STALE_CONTEXT,
    ),
    (
        frozenset(
            {
                "provider_outage",
                "gateway_failure",
                "upstream_error",
                "rate_limited",
                "provider_error",
            }
        ),
        RecoveryFailureClass.PROVIDER_OR_GATEWAY_FAILURE,
    ),
    (
        frozenset({"capability_deficit", "model_too_weak", "insufficient_capability"}),
        RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT,
    ),
    (
        frozenset({"tool_mismatch", "environment_mismatch", "tool_missing", "env_broken"}),
        RecoveryFailureClass.TOOL_OR_ENVIRONMENT_MISMATCH,
    ),
    (
        frozenset({"bad_decomposition", "bad_plan", "plan_failure", "circular_plan"}),
        RecoveryFailureClass.BAD_DECOMPOSITION_OR_PLAN,
    ),
    (
        frozenset({"implementation_error", "code_error", "logic_bug"}),
        RecoveryFailureClass.IMPLEMENTATION_ERROR,
    ),
    (
        frozenset({"verification_infra", "test_infra", "proof_env_failure"}),
        RecoveryFailureClass.VERIFICATION_OR_TEST_INFRA_FAILURE,
    ),
)

_PROVIDER_ERROR_CLASSES = frozenset(
    {
        "rate_limited",
        "quota_exhausted",
        "timeout",
        "upstream_error",
        "server_error",
        "provider_error",
        "gateway_error",
    }
)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def classify_failure(evidence: FailureEvidence) -> RecoveryFailureClass:
    """Deterministic failure classification from recorded evidence."""

    if evidence.cancelled or evidence.side_effects_ambiguous:
        return RecoveryFailureClass.AMBIGUOUS_OR_UNKNOWN

    signals = {s.lower() for s in evidence.signals}
    for group, failure_class in _SIGNAL_CLASS:
        if signals & group:
            return failure_class

    error = (evidence.error_class or "").lower().strip()
    message = (evidence.message or "").lower()

    if error in {"context_stale", "context_missing", "stale_context", "missing_context"}:
        return RecoveryFailureClass.MISSING_OR_STALE_CONTEXT
    if any(token in message for token in ("missing context", "stale context", "context pack")):
        return RecoveryFailureClass.MISSING_OR_STALE_CONTEXT

    if (
        error in _PROVIDER_ERROR_CLASSES
        or (evidence.status_code is not None and evidence.status_code in _RETRYABLE_STATUS_CODES)
        or "provider" in message
        or "gateway" in message
    ):
        return RecoveryFailureClass.PROVIDER_OR_GATEWAY_FAILURE

    if any(token in message for token in ("capability", "too weak", "cannot satisfy")):
        return RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT
    if any(token in message for token in ("tool", "sandbox", "environment", "binary missing")):
        return RecoveryFailureClass.TOOL_OR_ENVIRONMENT_MISMATCH
    if any(token in message for token in ("plan", "decomposition", "circular")):
        return RecoveryFailureClass.BAD_DECOMPOSITION_OR_PLAN
    if any(token in message for token in ("test runner", "verification infra", "proof env")):
        return RecoveryFailureClass.VERIFICATION_OR_TEST_INFRA_FAILURE
    if error in {"implementation_error", "code_error"} or "assertion" in message:
        return RecoveryFailureClass.IMPLEMENTATION_ERROR

    return RecoveryFailureClass.AMBIGUOUS_OR_UNKNOWN


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _component_ready(
    report: RuntimeCertificationReport | None,
    *,
    component_id: str | None,
    kinds: set[ComponentKind],
) -> bool:
    if report is None or not component_id:
        return True
    for item in report.components:
        if item.component_id == component_id and item.kind in kinds:
            return item.state is CertificationState.READY
        if item.identity == component_id and item.kind in kinds:
            return item.state is CertificationState.READY
    # Unknown component in evidence → treat as not certified-ready for switches.
    return False


def _pick_equivalent(
    current: ExecutionRoute,
    candidates: Sequence[ExecutionRoute],
    certification: RuntimeCertificationReport | None,
) -> ExecutionRoute | None:
    for candidate in candidates:
        if candidate.capability_tier != current.capability_tier:
            continue
        if candidate.is_frontier and not current.is_frontier:
            continue
        if (
            candidate.model_id == current.model_id
            and candidate.provider == current.provider
            and candidate.gateway_id == current.gateway_id
        ):
            continue
        if candidate.gateway_id and not _component_ready(
            certification, component_id=candidate.gateway_id, kinds={ComponentKind.GATEWAY}
        ):
            continue
        provider_listed = certification is not None and any(
            c.component_id == candidate.provider or c.identity == candidate.provider
            for c in certification.components
        )
        if provider_listed and not _component_ready(
            certification,
            component_id=candidate.provider,
            kinds={ComponentKind.PROVIDER, ComponentKind.GATEWAY},
        ):
            continue
        return candidate
    return None


def _pick_stronger(
    current: ExecutionRoute, candidates: Sequence[ExecutionRoute]
) -> ExecutionRoute | None:
    stronger = [
        c
        for c in candidates
        if c.capability_tier > current.capability_tier
        or (c.capability_tier == current.capability_tier and c.model_id != current.model_id)
    ]
    if not stronger:
        return None
    stronger.sort(key=lambda c: (c.capability_tier, c.is_frontier, c.model_id, c.provider))
    return stronger[0]


class BoundedRecoveryController:
    """Classify failures and select bounded corrective actions."""

    def __init__(self, *, bounds: RecoveryBounds | None = None) -> None:
        self.bounds = bounds or RecoveryBounds()
        self._history: list[RecoveryDecision] = []

    @property
    def history(self) -> tuple[RecoveryDecision, ...]:
        return tuple(self._history)

    def decide(
        self,
        *,
        evidence: FailureEvidence,
        route: ExecutionRoute,
        ledger: CostLedger,
        certification: RuntimeCertificationReport | None = None,
        equivalent_routes: Sequence[ExecutionRoute] = (),
        stronger_routes: Sequence[ExecutionRoute] = (),
        attempt_index: int | None = None,
        estimated_cash_usd: Decimal | None = None,
        estimated_tokens: Decimal | None = None,
        now: datetime | None = None,
    ) -> RecoveryDecision:
        when = _utc(now or datetime.now(timezone.utc))
        attempt = len(self._history) if attempt_index is None else attempt_index

        if (
            evidence.generating_worker_id is not None
            and evidence.verification_worker_id is not None
            and evidence.generating_worker_id == evidence.verification_worker_id
        ):
            raise ValueError(
                "verification must be independent of the generating worker "
                f"(got {evidence.generating_worker_id!r})"
            )

        cash_remaining = ledger.remaining_cash_usd()
        cash_remaining_str = None if cash_remaining is None else str(cash_remaining)
        digest = evidence.evidence_digest()
        cash_needed = (
            Decimal(str(estimated_cash_usd))
            if estimated_cash_usd is not None
            else self.bounds.estimated_action_cash_usd
        )
        tokens_needed = (
            Decimal(str(estimated_tokens))
            if estimated_tokens is not None
            else self.bounds.estimated_action_tokens
        )

        def _block(reason: str, failure_class: RecoveryFailureClass) -> RecoveryDecision:
            decision = RecoveryDecision(
                failure_class=failure_class,
                action=RecoveryAction.BLOCK,
                outcome=RecoveryOutcome.BLOCKED,
                route_before=route,
                route_after=route,
                reason=reason,
                attempt_index=attempt,
                evidence_digest=digest,
                created_at=when,
                escalated=False,
                frontier_call=False,
                success=False,
                budget_cash_remaining_usd=cash_remaining_str,
            )
            self._history.append(decision)
            return decision

        if evidence.cancelled:
            decision = RecoveryDecision(
                failure_class=RecoveryFailureClass.AMBIGUOUS_OR_UNKNOWN,
                action=RecoveryAction.BLOCK,
                outcome=RecoveryOutcome.CANCELLED,
                route_before=route,
                route_after=route,
                reason="cancellation during verification is not completion",
                attempt_index=attempt,
                evidence_digest=digest,
                created_at=when,
                success=False,
                charge_generating_model=False,
                budget_cash_remaining_usd=cash_remaining_str,
            )
            self._history.append(decision)
            return decision

        if self.bounds.deadline_at is not None and when > _utc(self.bounds.deadline_at):
            return _block("deadline exceeded; bounded blocker", classify_failure(evidence))

        if attempt >= self.bounds.max_attempts:
            return _block(
                f"max attempts ({self.bounds.max_attempts}) exhausted; bounded blocker",
                classify_failure(evidence),
            )

        if cash_remaining is not None and cash_needed > cash_remaining:
            return _block(
                "insufficient cash budget for recovery action; bounded blocker",
                classify_failure(evidence),
            )

        # Token/quota budget when a tokens pool is configured.
        if "tokens" in ledger.quota_budgets:
            quota_budget = ledger.quota_budgets["tokens"]
            # Peek reserved via a transient reserve attempt is invasive; use budget map.
            # Remaining = budget - reserved; access via reserve probe would mutate.
            # Use to_dict snapshot of reservations for tokens pool.
            reserved_tokens = Decimal("0")
            for reservation in ledger.reservations():
                if reservation.pool == "quota" and reservation.pool_id == "tokens":
                    if reservation.status in {"reserved", "partial"}:
                        reserved_tokens += reservation.amount
                    elif (
                        reservation.status == "reconciled" and reservation.actual_amount is not None
                    ):
                        reserved_tokens += reservation.actual_amount
            if tokens_needed > quota_budget - reserved_tokens:
                return _block(
                    "insufficient token/quota budget for recovery action; bounded blocker",
                    classify_failure(evidence),
                )

        failure_class = classify_failure(evidence)

        if failure_class is RecoveryFailureClass.AMBIGUOUS_OR_UNKNOWN:
            return _block(
                "ambiguous_or_unknown failure; conservative policy never marks success",
                failure_class,
            )

        action = RecoveryAction.BLOCK
        route_after = route
        reason = ""
        escalated = False
        frontier_call = False
        replan_signal = False
        charge_generating = True
        outcome = RecoveryOutcome.CONTINUE

        if failure_class is RecoveryFailureClass.MISSING_OR_STALE_CONTEXT:
            action = RecoveryAction.REHYDRATE
            route_after = route
            frontier_call = False
            reason = "rehydrate missing/stale context; retry same cheap route (no frontier call)"

        elif failure_class is RecoveryFailureClass.TOOL_OR_ENVIRONMENT_MISMATCH:
            action = RecoveryAction.REPAIR_TOOL_OR_ENVIRONMENT
            reason = "repair/select eligible tool or environment path without model upgrade"

        elif failure_class is RecoveryFailureClass.VERIFICATION_OR_TEST_INFRA_FAILURE:
            action = RecoveryAction.REPAIR_TOOL_OR_ENVIRONMENT
            charge_generating = False
            reason = (
                "reconcile verification/test infrastructure before charging the generating model"
            )

        elif failure_class is RecoveryFailureClass.PROVIDER_OR_GATEWAY_FAILURE:
            equivalent = _pick_equivalent(route, equivalent_routes, certification)
            if equivalent is None:
                return _block(
                    "provider/gateway failure with no equivalent same-tier certified route",
                    failure_class,
                )
            action = RecoveryAction.SWITCH_EXECUTION_PLANE
            route_after = equivalent
            reason = (
                "switch execution plane preserving capability tier "
                f"({route.capability_tier}); strategy/context/proof state unchanged"
            )

        elif failure_class is RecoveryFailureClass.IMPLEMENTATION_ERROR:
            action = RecoveryAction.RETRY
            reason = "bounded same-tier retry for implementation error"

        elif failure_class is RecoveryFailureClass.BAD_DECOMPOSITION_OR_PLAN:
            action = RecoveryAction.REPLAN
            replan_signal = True
            reason = (
                "emit replan signal for BOD-104 strategy planner within budget "
                "(planner not invoked here)"
            )

        elif failure_class is RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT:
            stronger = _pick_stronger(route, stronger_routes)
            if stronger is None:
                return _block(
                    "model capability deficit with no stronger qualified candidate", failure_class
                )
            action = RecoveryAction.ESCALATE_CAPABILITY
            route_after = stronger
            escalated = True
            frontier_call = stronger.is_frontier
            reason = (
                "escalate to stronger qualified candidate; "
                "do not endlessly retry incapable same-tier model"
            )

        else:
            return _block("unhandled failure class; conservative block", failure_class)

        if (
            route_after.is_frontier
            and failure_class is RecoveryFailureClass.MISSING_OR_STALE_CONTEXT
        ):
            route_after = ExecutionRoute(
                model_id=route.model_id,
                provider=route.provider,
                gateway_id=route.gateway_id,
                capability_tier=route.capability_tier,
                is_frontier=False,
            )
            frontier_call = False

        decision = RecoveryDecision(
            failure_class=failure_class,
            action=action,
            outcome=outcome,
            route_before=route,
            route_after=route_after,
            reason=reason,
            attempt_index=attempt,
            evidence_digest=digest,
            created_at=when,
            escalated=escalated,
            frontier_call=frontier_call,
            replan_signal=replan_signal,
            charge_generating_model=charge_generating,
            success=False,
            budget_cash_remaining_usd=cash_remaining_str,
        )
        self._history.append(decision)
        return decision


__all__ = [
    "BOUNDED_RECOVERY_SCHEMA_VERSION",
    "BoundedRecoveryController",
    "ExecutionRoute",
    "FailureEvidence",
    "RecoveryAction",
    "RecoveryBounds",
    "RecoveryDecision",
    "RecoveryFailureClass",
    "RecoveryOutcome",
    "classify_failure",
]
