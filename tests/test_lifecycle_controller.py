"""Acceptance tests for the lifecycle enforcement gate (feature 004, US2 / SC-002).

These exercise :meth:`LifecycleController._run_verification_hooks` directly — the
pure verification surface — so they are credential-free and need no real
RufloAdapter network. They prove:

- SC-002: an excluded-provider step is blocked at the verification gate with a
  ``decision_id``-traceable denial (FR-002).
- The pre-004 ``passed=True`` placeholder is replaced whenever an authority
  context (or ``decision_record``) is supplied.
- Backwards-compat: with no enforcement context the plan-level verification
  preserves the legacy ``passed=True`` placeholder result.
- Extra enforcement gates are detected via ``inspect.signature`` and threaded
  with ``decision_record``/``context``; legacy gates keep the old signature.
- An explicitly-registered enforcement gate fails closed (``decision_missing``)
  when no context is supplied (NFR-002) — fail-closed never optimistically
  admits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from verdict.decision_kernel_demo import build_demo_decision, degraded_decision, denied_decision
from verdict.enforcement import EnforcementContext, EnforcementVerificationGate
from verdict.lifecycle_controller import LifecycleConfig, LifecycleController


class _FakeAdapter:
    """Minimal adapter satisfying WorkflowCompiler's ``capability_manifest`` read."""

    capability_manifest = None


def _controller(*, extra_gates=None) -> LifecycleController:
    lc = LifecycleController(_FakeAdapter())
    lc.config = LifecycleConfig(
        use_workflow_plan_verification=True,
        run_verification_on_submit=False,
        run_verification_on_complete=False,
        extra_verification_gates=list(extra_gates or []),
    )
    return lc


def _plan(target_provider: str | None) -> SimpleNamespace:
    return SimpleNamespace(verification={"checks": []}, target_provider=target_provider)


# ---------------------------------------------------------------------------
# SC-002 — the verification gate blocks an excluded-provider step
# ---------------------------------------------------------------------------


class TestExcludedProviderBlocked:
    def test_plan_verification_blocks_excluded_provider(self) -> None:
        record = degraded_decision()
        excluded = record.exclusions[0]["model_id"]
        lc = _controller()
        deny = lc._run_verification_hooks(
            _plan(excluded), stage="pre_dispatch", decision_record=record
        )
        plan_result = deny[0]
        assert plan_result["passed"] is False
        assert plan_result["reason"] == "decision_denied_provider"
        assert plan_result["blocked_provider"] == excluded
        assert plan_result["decision_id"] == record.decision_id
        assert plan_result["admitted_set"] == [record.admitted[0].id]

    def test_plan_verification_admits_admitted_provider(self) -> None:
        record = build_demo_decision()
        admitted = record.admitted[0].id
        lc = _controller()
        allow = lc._run_verification_hooks(
            _plan(admitted), stage="pre_dispatch", decision_record=record
        )
        assert allow[0]["passed"] is True
        assert allow[0]["blocked_provider"] is None

    def test_excluded_provider_blocked_via_explicit_context(self) -> None:
        record = denied_decision()
        excluded = record.exclusions[0]["model_id"]
        ctx = EnforcementContext(
            decision_record=record, created_at=datetime(2026, 8, 20, tzinfo=timezone.utc)
        )
        lc = _controller()
        deny = lc._run_verification_hooks(_plan(excluded), stage="pre_dispatch", context=ctx)
        assert deny[0]["passed"] is False
        assert deny[0]["reason"] == "decision_denied_provider"

    def test_extra_enforcement_gate_blocks_excluded_provider(self) -> None:
        record = degraded_decision()
        excluded = record.exclusions[0]["model_id"]
        ctx = EnforcementContext(
            decision_record=record, created_at=datetime(2026, 8, 20, tzinfo=timezone.utc)
        )
        lc = _controller(extra_gates=[EnforcementVerificationGate()])
        results = lc._run_verification_hooks(_plan(excluded), stage="pre_dispatch", context=ctx)
        gate_result = next(r for r in results if r.get("source") == "enforcement_kernel")
        assert gate_result["passed"] is False
        assert gate_result["blocked_provider"] == excluded
        assert gate_result["reason"] == "decision_denied_provider"


# ---------------------------------------------------------------------------
# Backwards compat — placeholder remains when no authority is supplied
# ---------------------------------------------------------------------------


class TestBackwardsCompatPlaceholder:
    def test_plan_placeholder_remains_passed_without_context(self) -> None:
        lc = _controller()
        plan = _plan("omniroute/sonnet")
        # No decision_record and no context → legacy placeholder, untouched.
        results = lc._run_verification_hooks(plan, stage="pre_dispatch")
        assert results[0]["passed"] is True
        assert "placeholder" not in results[0].get("message", "")  # message normalized
        assert results[0]["source"] == "workflow_plan"

    def test_legacy_extra_gate_kept_on_two_arg_signature(self) -> None:
        """A gate that declares only workflow_plan/stage is called the old way."""

        class LegacyGate:
            def evaluate(self, *, workflow_plan, stage) -> dict:
                return {"passed": True, "source": "legacy_gate"}

        record = denied_decision()
        excluded = record.exclusions[0]["model_id"]
        lc = _controller(extra_gates=[LegacyGate()])
        results = lc._run_verification_hooks(
            _plan(excluded), stage="pre_dispatch", decision_record=record
        )
        legacy = next(r for r in results if r.get("source") == "legacy_gate")
        assert legacy["passed"] is True  # never receives the decision

    def test_explicit_enforcement_gate_fails_closed_without_context(self) -> None:
        """NFR-002: an enforcement gate with no decision denies (does not allow)."""
        lc = _controller(extra_gates=[EnforcementVerificationGate()])
        results = lc._run_verification_hooks(_plan("omniroute/sonnet"), stage="pre_dispatch")
        gate_result = next(r for r in results if r.get("source") == "enforcement_kernel")
        assert gate_result["passed"] is False
        assert gate_result["reason"] == "decision_missing"


# ---------------------------------------------------------------------------
# Fixture sanity — degraded decision shape used throughout
# ---------------------------------------------------------------------------


class TestFixtureShape:
    def test_degraded_decision_admits_one_excludes_one(self) -> None:
        record = degraded_decision()
        assert record.outcome == "degraded"
        assert len(record.admitted) == 1
        assert len(record.exclusions) == 1
        assert record.admitted[0].id == "omniroute/haiku"
        assert record.exclusions[0]["model_id"] == "omniroute/sonnet"
        assert record.decision_id.startswith("sha256:")
