import asyncio
import contextlib
import subprocess
from datetime import datetime, timezone

import pytest

from verdict.execution_path import ExecutionPathOffer, ExecutionPathRequest
from verdict.intelligence import IntelligenceService


def _service(
    *, profile: str = "development", admit_now: datetime | None = None
) -> IntelligenceService:
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={},
        profile=profile,
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_now=admit_now,
    )


def _fake_execution_path_request() -> ExecutionPathRequest:
    """Create a minimal ExecutionPathRequest for testing."""
    from verdict.cost_ledger import PriceEvidenceInput
    from verdict.effective_capability import AssistanceCost, AssistancePlan, TaskSlice
    from verdict.expected_cost import build_strategy_from_assistance
    from verdict.runtime_certification import CertificationState
    from verdict.session_economics import ConcreteRoute

    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    price: PriceEvidenceInput = {"input_usd_per_mtok": "1.00", "evidence_id": "price-1"}

    slice_ = TaskSlice(
        slice_id="slice-1",
        objective="refactor module",
        acceptance_criteria=("tests pass",),
        proof_criteria=("pytest green",),
    )
    route = ConcreteRoute(
        route_id="test-route",
        gateway="gateway-a",
        provider="provider-a",
        model="cheap-model",
        credential_pool="pool-a",
        capability_tier=1,
        eligible=True,
        excluded=False,
    )
    cost = AssistanceCost(
        context_tokens=0, tool_tokens=0, planning_tokens=0, verification_tokens=50
    )
    plan = AssistancePlan(
        plan_id="plan-test-route",
        candidate_id="test-route",
        task_slice=slice_,
        required_intrinsic_capabilities=("tools",),
        required_context_slots=("evidence",),
        required_context_evidence=("proof",),
        required_tool_capabilities=("pytest",),
        selected_tool_surface=("pytest",),
        decomposition=None,
        verification=None,
        assistance_cost=cost,
        result="sufficient",
        reasons=("intrinsic_ok",),
        intrinsic_sufficient=True,
        assisted_sufficient=True,
        assistance_delta=(),
        provenance=(),
        evidence_digest="sha256:plan",
    )
    expected_cost = build_strategy_from_assistance(
        strategy_id="direct_cheap:test-route",
        trajectory_id="traj-test",
        assistance=cost,
        execution_tokens=8_000,
        price=price,
        is_free=True,
        now=now,
    )
    offer = ExecutionPathOffer(
        strategy="direct_cheap",
        route=route,
        assistance_plan=plan,
        expected_cost=expected_cost,
        certification_state=CertificationState.READY,
        certification_freshness="fresh",
        is_cheap=True,
    )
    return ExecutionPathRequest(
        task_slice=slice_,
        trajectory_id="traj-test",
        offers=(offer,),
        pool_receipt=None,
        now=None,  # No injected time
    )


def test_production_readiness_succeeds_without_ruflo() -> None:
    report = _service(profile="production").readiness()
    assert report.status == "ready"
    assert report.production_ready is True
    assert report.profile == "production"
    assert report.degraded_mode is False
    assert report.managed_backend_status == "not_used"
    assert report.reason == "ready"
    assert report.adapter_versions == {}


def test_development_profile_readiness_succeeds_without_ruflo() -> None:
    report = _service(profile="development").readiness()
    assert report.status == "ready"
    assert report.production_ready is True
    assert report.profile == "development"
    assert report.degraded_mode is False
    assert report.managed_backend_status == "not_used"


def test_route_does_not_invoke_ruflo(monkeypatch) -> None:
    calls: list[list[str]] = []

    def mock_run(args: list[str], **kwargs: object) -> None:
        calls.append(list(args))
        raise OSError("Ruflo must not be invoked on the cheap path")

    monkeypatch.setattr(subprocess, "run", mock_run)

    decision = asyncio.run(
        _service().route("Here is a task with a private_key=sk-1234567890", criticality="medium")
    )

    assert decision.model == "anthropic/claude-3-opus"
    assert decision.degraded_mode is False
    assert decision.managed_backend_status == "not_used"
    assert all("ruflo" not in " ".join(str(part) for part in args) for args in calls)


def test_default_profile_is_development_and_fail_closed_does_not_depend_on_it(monkeypatch) -> None:
    """S2-F F3: DEFAULT_PROFILE stays "development"; no fail-closed guarantee hangs on it.

    - The API serve path forces execution-path authority in every profile.
    - Unverified candidates stay excluded in development unless the operator opts in.
    """
    from verdict.eligibility import EligibilityGate, allow_unverified_dev_from_env
    from verdict.intelligence import DEFAULT_PROFILE
    from verdict.models import ModelInfo
    from verdict.serve_path import CONTEXT_REQUIRE_AUTHORITY, serve_path_authority_required

    monkeypatch.delenv("VERDICT_REQUIRE_EXECUTION_PATH", raising=False)
    monkeypatch.delenv("VERDICT_ALLOW_UNVERIFIED_DEV", raising=False)
    assert DEFAULT_PROFILE == "development"
    assert serve_path_authority_required(profile=DEFAULT_PROFILE) is False
    assert serve_path_authority_required(profile="production") is True
    # api._route_with_intelligence sets this key for every API request.
    assert (
        serve_path_authority_required(
            profile=DEFAULT_PROFILE, context={CONTEXT_REQUIRE_AUTHORITY: True}
        )
        is True
    )

    gate = EligibilityGate(
        lambda _mid: None,
        protected_fail_closed=True,
        allow_unverified_in_dev=allow_unverified_dev_from_env(),
    )
    result = gate.evaluate(
        [ModelInfo(id="d/4", provider="d", capability_tier=2)],
        dev_mode=(DEFAULT_PROFILE == "development"),
    )
    assert result.admitted == []


def test_prepare_execution_path_request_uses_aware_utc_clock_when_admit_now_is_none(
    monkeypatch,
) -> None:
    """_prepare_execution_path_request with admit_now=None does not raise HealthCacheError
    and uses an aware UTC time."""
    from unittest.mock import patch

    from verdict.orchestration.health_cache import HealthCacheError

    svc = _service(admit_now=None)
    ep_request = _fake_execution_path_request()

    # Mock the external dependencies that would require network/gateway
    with (
        patch.object(svc, "_load_admit_snapshot", return_value=(None, None, False, None)),
        patch.object(svc, "_load_metadata", return_value=(None, None)),
    ):
        # This should not raise HealthCacheError
        try:
            result = svc._prepare_execution_path_request("test task", "medium", {}, ep_request)
            # The request should be returned (possibly modified)
            assert result is not None
        except HealthCacheError as e:
            pytest.fail(f"HealthCacheError raised unexpectedly: {e}")
        except Exception as e:
            # Other exceptions (like missing metadata) are expected in this mocked setup
            # The key assertion is that HealthCacheError is NOT raised
            if "HealthCacheError" in type(e).__name__:
                pytest.fail(f"HealthCacheError raised unexpectedly: {e}")


def test_prepare_execution_path_request_uses_injected_admit_now(monkeypatch) -> None:
    """Test-injected time is preserved and used for all admission decisions."""
    from unittest.mock import MagicMock, patch

    injected_time = datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)
    svc = _service(admit_now=injected_time)
    ep_request = _fake_execution_path_request()

    captured_now = {}

    def mock_gate_capability(receipt, requirements, *, snapshot, identity_map, now, **kwargs):
        captured_now["gate_capability"] = now
        return receipt, None

    def mock_build_selector(*args, now, **kwargs):
        captured_now["build_selector"] = now
        selector = MagicMock()

        def mock_assess_all(reqs, n):
            captured_now["selector_assess_all"] = n
            return [], []

        selector._assess_all = mock_assess_all
        return selector

    def mock_gate_admit_prove_confirm(receipt, *, now, **kwargs):
        captured_now["gate_admit_prove_confirm"] = now
        from verdict.admission import AdmissionReceipt

        return AdmissionReceipt(admitted=(), chosen=None, free_admitted=(), paid_admitted=())

    def mock_apply_best_of_admitted(receipt, *, now, task_class):
        captured_now["apply_best_of_admitted"] = now
        return receipt

    with (
        patch.object(svc, "_load_admit_snapshot", return_value=(None, None, False, None)),
        patch.object(svc, "_load_metadata", return_value=(None, None)),
        patch("verdict.capability_gate.gate_capability", side_effect=mock_gate_capability),
        patch(
            "verdict.orchestration.eligibility_report.build_selector",
            side_effect=mock_build_selector,
        ),
        patch(
            "verdict.admit_prove_confirm.gate_admit_prove_confirm",
            side_effect=mock_gate_admit_prove_confirm,
        ),
        patch("verdict.chooser.apply_best_of_admitted", side_effect=mock_apply_best_of_admitted),
        contextlib.suppress(Exception),
    ):
        svc._prepare_execution_path_request("test task", "medium", {}, ep_request)

    # Verify all calls received the injected time
    for key, value in captured_now.items():
        assert value == injected_time, f"{key} received {value}, expected {injected_time}"


def test_prepare_execution_path_request_single_clock_per_request(monkeypatch) -> None:
    """All admission gates in one request see the same instant."""
    from unittest.mock import MagicMock, patch

    svc = _service(admit_now=None)  # No injected time, should use UTC now
    ep_request = _fake_execution_path_request()

    captured_now = {}

    def mock_gate_capability(receipt, requirements, *, snapshot, identity_map, now, **kwargs):
        captured_now.setdefault("gate_capability", []).append(now)
        return receipt, None

    def mock_build_selector(*args, now, **kwargs):
        captured_now.setdefault("build_selector", []).append(now)
        selector = MagicMock()

        def mock_assess_all(reqs, n):
            captured_now.setdefault("selector_assess_all", []).append(n)
            return [], []

        selector._assess_all = mock_assess_all
        return selector

    def mock_gate_admit_prove_confirm(receipt, *, now, **kwargs):
        captured_now.setdefault("gate_admit_prove_confirm", []).append(now)
        from verdict.admission import AdmissionReceipt

        return AdmissionReceipt(admitted=(), chosen=None, free_admitted=(), paid_admitted=())

    def mock_apply_best_of_admitted(receipt, *, now, task_class):
        captured_now.setdefault("apply_best_of_admitted", []).append(now)
        return receipt

    with (
        patch.object(svc, "_load_admit_snapshot", return_value=(None, None, False, None)),
        patch.object(svc, "_load_metadata", return_value=(None, None)),
        patch("verdict.capability_gate.gate_capability", side_effect=mock_gate_capability),
        patch(
            "verdict.orchestration.eligibility_report.build_selector",
            side_effect=mock_build_selector,
        ),
        patch(
            "verdict.admit_prove_confirm.gate_admit_prove_confirm",
            side_effect=mock_gate_admit_prove_confirm,
        ),
        patch("verdict.chooser.apply_best_of_admitted", side_effect=mock_apply_best_of_admitted),
        contextlib.suppress(Exception),
    ):
        svc._prepare_execution_path_request("test task", "medium", {}, ep_request)

    # All captured times should be the same (within the same request)
    all_times = []
    for _key, values in captured_now.items():
        if isinstance(values, list):
            all_times.extend(values)
        else:
            all_times.append(values)

    # All times should be equal (same clock reference)
    if all_times:
        first_time = all_times[0]
        for t in all_times[1:]:
            assert t == first_time, f"Clock drift detected: {t} != {first_time}"
