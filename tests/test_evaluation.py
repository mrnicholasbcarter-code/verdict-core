"""Tests for evidence-gated shadow and counterfactual evaluation (#119)."""

from datetime import datetime, timedelta, timezone

import pytest

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
)
from verdict.evaluation import (
    EvaluationCase,
    EvaluationController,
    EvaluationError,
    EvaluationFailureClass,
    EvaluationObservation,
    EvaluationReport,
    EvaluationStatus,
    EvaluationSuite,
    EvaluationVariant,
    PromotionPolicy,
    PromotionState,
    VerificationStatus,
    build_evaluation_report,
    counterfactual_from_receipt,
    normalize_failure_class,
)
from verdict.receipt_store import ReceiptStore


def _route(model: str = "provider/model-a") -> RouteIdentity:
    return RouteIdentity(
        gateway="fake-gateway",
        provider="provider",
        connection="test-account",
        endpoint="http://127.0.0.1:9000/v1",
        protocol="responses",
        model_id=model,
    )


def _passport(route: RouteIdentity) -> CapabilityPassport:
    now = datetime.now(timezone.utc)
    evidence = CapabilityEvidence(
        status=CapabilityStatus.SUPPORTED,
        source="test-probe",
        observed_at=now,
        expires_at=now + timedelta(hours=1),
        confidence=1.0,
        evidence_digest="sha256:" + "a" * 64,
        authority=EvidenceAuthority.OBSERVED,
    )
    return CapabilityPassport(
        route_identity=route,
        qualified_at=now,
        expires_at=now + timedelta(hours=1),
        claimed={"tools": evidence},
        observed={"tools": evidence},
    )


def _suite() -> EvaluationSuite:
    cases = []
    for variant in (EvaluationVariant.NO_CONTEXT, EvaluationVariant.CONTEXT_PACK):
        cases.extend(
            [
                EvaluationCase(f"{variant.value}-1", "task-1", variant, 1, 100, heldout=False),
                EvaluationCase(f"{variant.value}-holdout", "task-2", variant, 2, 100, heldout=True),
            ]
        )
    return EvaluationSuite("suite-v1", tuple(cases))


def _observations(
    route: RouteIdentity, *, context_score: float = 0.9
) -> list[EvaluationObservation]:
    result = []
    for case in _suite().cases:
        score = 0.7 if case.variant is EvaluationVariant.NO_CONTEXT else context_score
        result.append(
            EvaluationObservation(
                route_identity=route,
                case_id=case.case_id,
                task_fingerprint=case.task_fingerprint,
                variant=case.variant,
                seed=case.seed,
                status=EvaluationStatus.SUCCESS,
                verification=VerificationStatus.PASSED,
                quality_score=score,
                failure_class=EvaluationFailureClass.NONE,
                verification_receipt_id=f"verify-{case.case_id}",
                latency_ms=10,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                metadata={"heldout": case.heldout},
            )
        )
    return result


def _passport_digest(route: RouteIdentity) -> str:
    return _passport(route).digest


def test_report_is_paired_and_reports_confidence_and_failure_separation() -> None:
    route = _route()
    report = build_evaluation_report(
        _suite(), route, _observations(route), passport_digest=_passport_digest(route)
    )

    assert report.verify()
    assert report.summaries[EvaluationVariant.CONTEXT_PACK].quality_score == pytest.approx(0.9)
    assert report.summaries[EvaluationVariant.CONTEXT_PACK].coverage == 1.0
    assert report.summaries[EvaluationVariant.CONTEXT_PACK].confidence_low >= 0
    assert report.heldout_case_count == 2


def test_failure_taxonomy_normalizes_protocol_labels() -> None:
    assert normalize_failure_class("unauthorized") is EvaluationFailureClass.AUTHENTICATION
    assert normalize_failure_class("schema_invalid") is EvaluationFailureClass.CAPABILITY
    assert normalize_failure_class("rate-limited") is EvaluationFailureClass.QUOTA


def test_report_rejects_missing_or_cross_route_observations() -> None:
    route = _route()
    observations = _observations(route)
    with pytest.raises(EvaluationError, match="incomplete"):
        build_evaluation_report(_suite(), route, observations[:-1])
    with pytest.raises(EvaluationError, match="route identity"):
        build_evaluation_report(
            _suite(), route, [*_observations(route)[:-1], _observations(_route("other/model"))[-1]]
        )


def test_operational_failures_are_not_quality_successes() -> None:
    route = _route()
    observations = _observations(route)
    observations[2] = EvaluationObservation(
        route_identity=route,
        case_id=observations[2].case_id,
        task_fingerprint=observations[2].task_fingerprint,
        variant=observations[2].variant,
        seed=observations[2].seed,
        status=EvaluationStatus.UNKNOWN,
        verification=VerificationStatus.NOT_RUN,
        failure_class=EvaluationFailureClass.AUTHENTICATION,
        verification_receipt_id=None,
    )
    report = build_evaluation_report(
        _suite(), route, observations, passport_digest=_passport_digest(route)
    )
    summary = report.summaries[EvaluationVariant.CONTEXT_PACK]
    assert summary.non_quality_failure_count == 1
    assert summary.quality_count == 1


def test_quality_failure_is_not_promotion_eligible() -> None:
    route = _route()
    observations = _observations(route)
    observations[0] = EvaluationObservation(
        route_identity=route,
        case_id=observations[0].case_id,
        task_fingerprint=observations[0].task_fingerprint,
        variant=observations[0].variant,
        seed=observations[0].seed,
        status=EvaluationStatus.FAILURE,
        verification=VerificationStatus.PASSED,
        verification_receipt_id="verify-failed",
        quality_score=0.99,
        failure_class=EvaluationFailureClass.QUALITY,
    )
    report = build_evaluation_report(
        _suite(), route, observations, passport_digest=_passport_digest(route)
    )
    assert report.summaries[EvaluationVariant.NO_CONTEXT].quality_count == 1
    assert report.summaries[EvaluationVariant.NO_CONTEXT].verified_count == 1


def test_promotion_requires_passport_and_heldout_evidence() -> None:
    route = _route()
    passport = _passport(route)
    controller = EvaluationController(ReceiptStore(":memory:", strict_scope=True))
    report = build_evaluation_report(
        _suite(), route, _observations(route), passport_digest=passport.digest
    )
    controller.record_report(report)
    policy = PromotionPolicy(minimum_context_lift=0.1)

    denied = controller.evaluate_promotion(
        report, policy, passport=None, required_capabilities=("tools",)
    )
    assert denied.allowed is False
    assert "passport" in " ".join(denied.reasons)

    allowed = controller.evaluate_promotion(
        report, policy, passport=passport, required_capabilities=("tools",)
    )
    assert allowed.allowed is True
    controller.promote(allowed)
    assert controller.state(route) is PromotionState.CANDIDATE


def test_promotion_lifecycle_kill_switch_and_rollback_are_fail_closed() -> None:
    route = _route()
    passport = _passport(route)
    controller = EvaluationController(ReceiptStore(":memory:", strict_scope=True))
    report = build_evaluation_report(
        _suite(), route, _observations(route), passport_digest=passport.digest
    )
    controller.record_report(report)
    policy = PromotionPolicy(minimum_context_lift=0.1)
    candidate = controller.evaluate_promotion(
        report, policy, passport=passport, required_capabilities=("tools",)
    )
    controller.promote(candidate)
    canary = controller.evaluate_promotion(
        report, policy, passport=passport, target_state=PromotionState.CANARY
    )
    controller.promote(canary)
    active = controller.evaluate_promotion(
        report, policy, passport=passport, target_state=PromotionState.ACTIVE
    )
    controller.promote(active)
    assert (
        controller.rollback(route, reason="drift detected", automatic=True)
        is PromotionState.DEGRADED
    )
    assert controller.kill_switch(route, reason="safety regression") is PromotionState.QUARANTINED
    denied = controller.evaluate_promotion(report, policy, passport=passport)
    assert denied.allowed is False
    assert "route is kill-switched" in denied.reasons


def test_counterfactual_is_replay_only_and_cannot_authorize_promotion() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    source = store.put_receipt(
        "execution",
        "evaluation",
        {"task_fingerprint": "task-1", "route_key": _route("provider/observed").key},
    )
    result = counterfactual_from_receipt(
        source.receipt_id,
        "task-1",
        _route("provider/observed"),
        _route("provider/counterfactual"),
        quality_score=0.99,
        verification=VerificationStatus.PASSED,
        receipt_store=store,
        scope="evaluation",
    )
    assert result.training_eligible is False
    assert result.promotion_eligible is False
    assert result.to_dict()["kind"] == "counterfactual"


def test_expired_report_cannot_promote() -> None:
    route = _route()
    passport = _passport(route)
    expired = datetime.now(timezone.utc) - timedelta(hours=2)
    observations = [
        EvaluationObservation(
            route_identity=item.route_identity,
            case_id=item.case_id,
            task_fingerprint=item.task_fingerprint,
            variant=item.variant,
            seed=item.seed,
            status=item.status,
            verification=item.verification,
            failure_class=item.failure_class,
            quality_score=item.quality_score,
            verification_receipt_id=item.verification_receipt_id,
            observed_at=expired,
            expires_at=expired + timedelta(minutes=1),
        )
        for item in _observations(route)
    ]
    report = build_evaluation_report(_suite(), route, observations, passport_digest=passport.digest)
    controller = EvaluationController(ReceiptStore(":memory:", strict_scope=True))
    controller.record_report(report)
    decision = controller.evaluate_promotion(report, PromotionPolicy(), passport=passport)
    assert "stale" in " ".join(decision.reasons)


def test_controller_counterfactual_requires_scoped_source_and_persists_child() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    controller = EvaluationController(store)
    source = store.put_receipt(
        "execution",
        "evaluation",
        {"task_fingerprint": "task-1", "route_key": _route("provider/observed").key},
    )
    result = controller.counterfactual(
        source_receipt_id=source.receipt_id,
        task_fingerprint="task-1",
        observed_route=_route("provider/observed"),
        counterfactual_route=_route("provider/counterfactual"),
        quality_score=0.5,
        verification=VerificationStatus.PASSED,
    )
    children = store.query_receipts(
        scope="evaluation", parent_receipt_id=source.receipt_id, limit=10
    )
    assert result.promotion_eligible is False
    assert len(children) == 1


def test_evaluation_artifacts_round_trip_and_reject_tampering() -> None:
    route = _route()
    suite = _suite()
    report = build_evaluation_report(
        suite, route, _observations(route), passport_digest=_passport_digest(route)
    )
    assert EvaluationSuite.from_dict(suite.to_dict()).digest == suite.digest
    assert (
        build_evaluation_report(
            EvaluationSuite.from_dict(suite.to_dict()),
            route,
            tuple(EvaluationObservation.from_dict(item.to_dict()) for item in _observations(route)),
            passport_digest=report.passport_digest,
        ).suite_digest
        == report.suite_digest
    )
    assert EvaluationReport.from_dict(report.to_dict()).digest == report.digest
    tampered = report.to_dict()
    tampered["observations"][0]["quality_score"] = 0.01
    with pytest.raises(EvaluationError, match="integrity"):
        EvaluationReport.from_dict(tampered)


def test_promotion_rejects_forged_decision_and_restart_reconstructs_state() -> None:
    route = _route()
    passport = _passport(route)
    store = ReceiptStore(":memory:", strict_scope=True)
    controller = EvaluationController(store)
    report = build_evaluation_report(
        _suite(), route, _observations(route), passport_digest=passport.digest
    )
    controller.record_report(report)
    decision = controller.evaluate_promotion(report, PromotionPolicy(), passport=passport)
    forged = type(decision)(
        allowed=True,
        target_state=decision.target_state,
        route_identity=decision.route_identity,
        report_digest=decision.report_digest,
        suite_digest=decision.suite_digest,
        passport_digest=decision.passport_digest,
        policy_digest=decision.policy_digest,
        reasons=(),
    )
    with pytest.raises(EvaluationError, match=r"stored|issued|durably"):
        controller.promote(forged)
    controller.promote(decision)
    restarted = EvaluationController(store)
    assert restarted.state(route) is PromotionState.CANDIDATE
    with pytest.raises(EvaluationError, match=r"stored|issued|durably"):
        restarted.promote(forged)


def test_counterfactual_rejects_mismatched_source_task_and_route() -> None:
    store = ReceiptStore(":memory:", strict_scope=True)
    observed = _route("provider/observed")
    source = store.put_receipt(
        "execution", "evaluation", {"task_fingerprint": "task-1", "route_key": observed.key}
    )
    with pytest.raises(EvaluationError, match="task fingerprint"):
        counterfactual_from_receipt(
            source.receipt_id,
            "task-2",
            observed,
            _route("provider/counterfactual"),
            quality_score=None,
            verification=VerificationStatus.UNKNOWN,
            receipt_store=store,
            scope="evaluation",
        )
    with pytest.raises(EvaluationError, match="route"):
        counterfactual_from_receipt(
            source.receipt_id,
            "task-1",
            _route("provider/other"),
            _route("provider/counterfactual"),
            quality_score=None,
            verification=VerificationStatus.UNKNOWN,
            receipt_store=store,
            scope="evaluation",
        )
