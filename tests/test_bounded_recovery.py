"""Proof fixtures for failure-directed bounded recovery (BOD-55)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from verdict.bounded_recovery import (
    BoundedRecoveryController,
    ExecutionRoute,
    FailureEvidence,
    RecoveryAction,
    RecoveryBounds,
    RecoveryFailureClass,
    RecoveryOutcome,
    classify_failure,
)
from verdict.cost_ledger import CostLedger
from verdict.runtime_certification import (
    CertificationState,
    CertifiedComponent,
    ComponentKind,
    RuntimeCertificationReport,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _cheap_route(**overrides: object) -> ExecutionRoute:
    base = {
        "model_id": "openrouter/free-small",
        "provider": "openrouter",
        "gateway_id": "gateway-a",
        "capability_tier": 0,
        "is_frontier": False,
    }
    base.update(overrides)
    return ExecutionRoute(**base)  # type: ignore[arg-type]


def _cert_report(*components: CertifiedComponent) -> RuntimeCertificationReport:
    return RuntimeCertificationReport(
        certified_at=NOW,
        expires_at=NOW + timedelta(seconds=300),
        ttl_seconds=300,
        components=components,
        memory_authority=None,
        conflicts=(),
        probes_used=0,
        premium_probes_used=0,
    )


def _component(
    component_id: str,
    kind: ComponentKind,
    state: CertificationState,
    *,
    identity: str | None = None,
) -> CertifiedComponent:
    return CertifiedComponent(
        component_id=component_id,
        kind=kind,
        identity=identity or component_id,
        state=state,
        source="fixture",
        confidence=1.0,
        freshness="fresh",
        observed_at=NOW,
        expires_at=NOW + timedelta(seconds=300),
    )


def _ledger(*, cash: str = "5.00") -> CostLedger:
    return CostLedger(
        trajectory_id="traj-bod-55",
        cash_budget_usd=Decimal(cash),
        quota_budgets={"tokens": Decimal("10000")},
        subscription_budgets={"codex-plus": Decimal("50")},
    )


def test_missing_context_rehydrates_same_cheap_model_no_frontier() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    route = _cheap_route()
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"missing_context"}),
            message="context pack missing required files",
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=route,
        ledger=_ledger(),
        certification=_cert_report(
            _component("gateway-a", ComponentKind.GATEWAY, CertificationState.READY)
        ),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.MISSING_OR_STALE_CONTEXT
    assert decision.action is RecoveryAction.REHYDRATE
    assert decision.outcome is RecoveryOutcome.CONTINUE
    assert decision.route_after == route
    assert decision.route_after.is_frontier is False
    assert decision.frontier_call is False
    assert decision.escalated is False


def test_provider_outage_switches_equivalent_same_tier() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    route = _cheap_route(gateway_id="gateway-a", provider="openrouter")
    alt = _cheap_route(gateway_id="gateway-b", provider="together", model_id="together/free-small")
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"provider_outage"}),
            error_class="upstream_error",
            status_code=503,
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=route,
        ledger=_ledger(),
        certification=_cert_report(
            _component("gateway-a", ComponentKind.GATEWAY, CertificationState.UNAVAILABLE),
            _component("gateway-b", ComponentKind.GATEWAY, CertificationState.READY),
            _component("together", ComponentKind.PROVIDER, CertificationState.READY),
        ),
        equivalent_routes=(alt,),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.PROVIDER_OR_GATEWAY_FAILURE
    assert decision.action is RecoveryAction.SWITCH_EXECUTION_PLANE
    assert decision.route_after == alt
    assert decision.route_after.capability_tier == route.capability_tier
    assert decision.escalated is False
    assert decision.frontier_call is False


def test_model_capability_deficit_escalates_not_same_tier_retry() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=5))
    route = _cheap_route()
    stronger = _cheap_route(
        model_id="anthropic/sonnet",
        provider="anthropic",
        gateway_id="gateway-a",
        capability_tier=2,
        is_frontier=False,
    )
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"capability_deficit"}),
            message="model cannot satisfy required reasoning depth",
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=route,
        ledger=_ledger(),
        certification=_cert_report(
            _component("gateway-a", ComponentKind.GATEWAY, CertificationState.READY)
        ),
        stronger_routes=(stronger,),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.MODEL_CAPABILITY_DEFICIT
    assert decision.action is RecoveryAction.ESCALATE_CAPABILITY
    assert decision.route_after == stronger
    assert decision.escalated is True
    assert decision.route_after.capability_tier > route.capability_tier


def test_bad_decomposition_emits_replan_signal_within_budget() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"bad_decomposition"}),
            message="plan steps circular",
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(cash="2.00"),
        certification=_cert_report(),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.BAD_DECOMPOSITION_OR_PLAN
    assert decision.action is RecoveryAction.REPLAN
    assert decision.outcome is RecoveryOutcome.CONTINUE
    assert decision.replan_signal is True
    assert "BOD-104" in decision.reason or "replan" in decision.reason.lower()
    assert decision.frontier_call is False


def test_budget_exhaustion_and_max_attempts_become_bounded_blocker() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=2))
    ledger = _ledger(cash="0.10")
    ledger.reserve(Decimal("0.10"), pool="cash", now=NOW)

    exhausted = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"implementation_error"}),
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=ledger,
        certification=_cert_report(),
        attempt_index=0,
        estimated_cash_usd=Decimal("0.05"),
        now=NOW,
    )
    assert exhausted.action is RecoveryAction.BLOCK
    assert exhausted.outcome is RecoveryOutcome.BLOCKED
    assert "budget" in exhausted.reason.lower()

    maxed = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=2)).decide(
        evidence=FailureEvidence(
            signals=frozenset({"implementation_error"}),
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(),
        certification=_cert_report(),
        attempt_index=2,
        now=NOW,
    )
    assert maxed.action is RecoveryAction.BLOCK
    assert maxed.outcome is RecoveryOutcome.BLOCKED
    assert "attempt" in maxed.reason.lower()


def test_cancellation_during_verification_is_not_success() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(
            cancelled=True,
            signals=frozenset({"cancelled"}),
            message="verification cancelled by operator",
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(),
        certification=_cert_report(),
        now=NOW,
    )
    assert decision.outcome is not RecoveryOutcome.SUCCESS
    assert decision.outcome in {RecoveryOutcome.BLOCKED, RecoveryOutcome.CANCELLED}
    assert decision.success is False


def test_ambiguous_never_marks_success() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset(),
            message="something went wrong",
            side_effects_ambiguous=True,
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(),
        certification=_cert_report(),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.AMBIGUOUS_OR_UNKNOWN
    assert decision.success is False
    assert decision.outcome is not RecoveryOutcome.SUCCESS
    assert decision.action is RecoveryAction.BLOCK


def test_classification_deterministic_and_replayable() -> None:
    evidence = FailureEvidence(
        signals=frozenset({"stale_context"}),
        error_class="context_stale",
        message="envelope expired",
        generating_worker_id="gen-1",
        verification_worker_id="verify-1",
    )
    first = classify_failure(evidence)
    second = classify_failure(evidence)
    assert first is second is RecoveryFailureClass.MISSING_OR_STALE_CONTEXT
    digest_a = evidence.evidence_digest()
    digest_b = evidence.evidence_digest()
    assert digest_a == digest_b
    assert digest_a.startswith("sha256:")


def test_verification_must_be_independent_of_generating_worker() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    with pytest.raises(ValueError, match="independent"):
        controller.decide(
            evidence=FailureEvidence(
                signals=frozenset({"implementation_error"}),
                generating_worker_id="same-worker",
                verification_worker_id="same-worker",
            ),
            route=_cheap_route(),
            ledger=_ledger(),
            certification=_cert_report(),
            now=NOW,
        )


def test_attempts_and_transitions_are_recorded() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=4))
    route = _cheap_route()
    alt = _cheap_route(gateway_id="gateway-b", provider="together")
    d1 = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"missing_context"}),
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=route,
        ledger=_ledger(),
        certification=_cert_report(
            _component("gateway-a", ComponentKind.GATEWAY, CertificationState.READY),
            _component("gateway-b", ComponentKind.GATEWAY, CertificationState.READY),
        ),
        attempt_index=0,
        now=NOW,
    )
    d2 = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"provider_outage"}),
            status_code=503,
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=route,
        ledger=_ledger(),
        certification=_cert_report(
            _component("gateway-a", ComponentKind.GATEWAY, CertificationState.UNAVAILABLE),
            _component("gateway-b", ComponentKind.GATEWAY, CertificationState.READY),
        ),
        equivalent_routes=(alt,),
        attempt_index=1,
        now=NOW + timedelta(seconds=1),
    )
    assert len(controller.history) == 2
    assert controller.history[0].action is RecoveryAction.REHYDRATE
    assert controller.history[1].action is RecoveryAction.SWITCH_EXECUTION_PLANE
    assert d1.to_dict()["failure_class"] == "missing_or_stale_context"
    assert d2.to_dict()["route_after"]["gateway_id"] == "gateway-b"


def test_deadline_exceeded_blocks() -> None:
    controller = BoundedRecoveryController(
        bounds=RecoveryBounds(max_attempts=5, deadline_at=NOW - timedelta(seconds=1))
    )
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"implementation_error"}),
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(),
        certification=_cert_report(),
        now=NOW,
    )
    assert decision.action is RecoveryAction.BLOCK
    assert "deadline" in decision.reason.lower()


def test_tool_mismatch_repairs_environment_not_escalates() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"tool_mismatch"}),
            message="pytest binary missing in sandbox",
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(),
        certification=_cert_report(
            _component("toolchain", ComponentKind.TOOLCHAIN, CertificationState.DEGRADED)
        ),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.TOOL_OR_ENVIRONMENT_MISMATCH
    assert decision.action is RecoveryAction.REPAIR_TOOL_OR_ENVIRONMENT
    assert decision.escalated is False


def test_verification_infra_failure_does_not_charge_generator() -> None:
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=3))
    decision = controller.decide(
        evidence=FailureEvidence(
            signals=frozenset({"verification_infra"}),
            message="test runner crashed before assertions",
            generating_worker_id="gen-1",
            verification_worker_id="verify-1",
        ),
        route=_cheap_route(),
        ledger=_ledger(),
        certification=_cert_report(),
        now=NOW,
    )
    assert decision.failure_class is RecoveryFailureClass.VERIFICATION_OR_TEST_INFRA_FAILURE
    assert decision.action is RecoveryAction.REPAIR_TOOL_OR_ENVIRONMENT
    assert decision.charge_generating_model is False
    assert decision.escalated is False
