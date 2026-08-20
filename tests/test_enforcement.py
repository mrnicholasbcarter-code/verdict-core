"""Acceptance tests for the native runtime enforcement kernel (feature 004).

Covers the single shared primitive ``check_enforcement`` (T005), the lifecycle
verification gate composition, and the gateway dispatch guard. Fixtures come
from :mod:`verdict.decision_kernel_demo` so these run credential-free with no
network and no wall-clock reliance (NFR-001); the only live clock use is the
injectable ``now`` for the expiry path.

The matrices below assert every leg of the fail-closed guarantee (NFR-002):
- all five ``EnforcementReason`` codes are reachable and stable;
- ``allowed`` is monotonically tied to a concrete decision-bound check, never
  to optimism;
- accepted / denied / degraded outcomes project the correct admitted /
  exclusion sets and decision ids (FR-005).
- determinism: identical inputs → identical :class:`EnforcementResult`.
- performance: a single ``check_enforcement`` call stays under 1 ms p99.
"""

from __future__ import annotations

import dataclasses
import statistics
from datetime import datetime, timezone

import pytest

from verdict.decision_kernel import DecisionRecord, verify_decision
from verdict.decision_kernel_demo import (
    build_demo_decision,
    degraded_decision,
    demo_inputs,
    denied_decision,
)
from verdict.enforcement import (
    EnforcementContext,
    EnforcementGatewayError,
    EnforcementReason,
    EnforcementResult,
    EnforcementVerificationGate,
    check_enforcement,
    dispatch_with_enforcement,
)


def _mut(record: DecisionRecord, **changes) -> DecisionRecord:
    """Return a copy of ``record`` with the given fields overridden (non-frozen-safe)."""
    return dataclasses.replace(record, **changes)


UTC = timezone.utc

ADMITTED_SONNET = "omniroute/sonnet"
ADMITTED_HAIKU = "omniroute/haiku"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(record: DecisionRecord, *, expires_at: datetime | None = None) -> EnforcementContext:
    return EnforcementContext(
        decision_record=record, created_at=datetime(2026, 8, 20, tzinfo=UTC), expires_at=expires_at
    )


@pytest.fixture
def accepted_record() -> DecisionRecord:
    return build_demo_decision()


@pytest.fixture
def denied_record() -> DecisionRecord:
    return denied_decision()


@pytest.fixture
def degraded_record() -> DecisionRecord:
    return degraded_decision()


def _first_exclusion(record: DecisionRecord) -> str:
    return record.exclusions[0]["model_id"]


# ---------------------------------------------------------------------------
# 1. Fail-closed matrix — every EnforcementReason is reachable and stable
# ---------------------------------------------------------------------------


class TestFailClosedMatrix:
    """NFR-002: missing/stale/malformed always deny; the five reasons are closed."""

    def test_missing_context_denies_with_decision_missing(self) -> None:
        result = check_enforcement(None, ADMITTED_SONNET)
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_MISSING
        assert result.decision_id == ""
        assert result.admitted_set == () and result.exclusions == ()

    def test_missing_record_on_context_denies_with_decision_missing(
        self, accepted_record: DecisionRecord
    ) -> None:
        # An EnforcementContext whose record is None is treated as missing.
        ctx = EnforcementContext(decision_record=None, created_at=datetime.now(UTC))  # type: ignore[arg-type]
        result = check_enforcement(ctx, ADMITTED_SONNET)
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_MISSING

    def test_empty_decision_id_denies_with_decision_malformed(
        self, accepted_record: DecisionRecord
    ) -> None:
        record = _mut(accepted_record, decision_id="")
        result = check_enforcement(_ctx(record), ADMITTED_SONNET)
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_MALFORMED
        assert result.decision_id == ""

    def test_non_string_decision_id_denies_with_decision_malformed(
        self, accepted_record: DecisionRecord
    ) -> None:
        record = _mut(accepted_record, decision_id=123)  # type: ignore[arg-type]
        result = check_enforcement(_ctx(record), ADMITTED_SONNET)
        assert result.reason is EnforcementReason.DECISION_MALFORMED

    def test_expired_context_denies_with_decision_expired(
        self, accepted_record: DecisionRecord
    ) -> None:
        # created_at precedes expires_at (valid invariant); the injected clock
        # is past expiry, so enforcement fails closed.
        ctx = _ctx(accepted_record, expires_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC))
        result = check_enforcement(ctx, ADMITTED_SONNET, now=datetime(2026, 8, 21, tzinfo=UTC))
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_EXPIRED
        assert result.decision_id == accepted_record.decision_id

    def test_not_expired_context_allows(self, accepted_record: DecisionRecord) -> None:
        ctx = _ctx(accepted_record, expires_at=datetime(2026, 8, 22, tzinfo=UTC))
        result = check_enforcement(ctx, ADMITTED_SONNET, now=datetime(2026, 8, 20, tzinfo=UTC))
        assert result.allowed is True

    def test_malformed_admitted_candidate_denies_with_decision_malformed(
        self, accepted_record: DecisionRecord
    ) -> None:
        bogus = type("C", (), {"id": None})()  # admitted id missing/None
        record = _mut(accepted_record, admitted=[bogus])
        result = check_enforcement(_ctx(record), ADMITTED_SONNET)
        assert result.reason is EnforcementReason.DECISION_MALFORMED

    def test_malformed_exclusion_entry_denies_with_decision_malformed(
        self, accepted_record: DecisionRecord
    ) -> None:
        record = _mut(accepted_record, exclusions=[{"not_model_id": "x"}], outcome="denied")
        result = check_enforcement(_ctx(record), ADMITTED_SONNET)
        assert result.reason is EnforcementReason.DECISION_MALFORMED

    def test_reason_set_is_closed(self) -> None:
        # FR-005: the vocabulary is exactly the five stable codes.
        assert {r.value for r in EnforcementReason} == {
            "decision_denied_provider",
            "decision_degraded_provider_not_admitted",
            "decision_missing",
            "decision_expired",
            "decision_malformed",
        }


# ---------------------------------------------------------------------------
# 2. Outcome-specific decision-bound logic
# ---------------------------------------------------------------------------


class TestDecisionBoundOutcomes:
    """FR-001/FR-006: hard admitted/exclusion sets are the sole input."""

    def test_accepted_admits_any_member_of_admitted_set(
        self, accepted_record: DecisionRecord
    ) -> None:
        result = check_enforcement(_ctx(accepted_record), ADMITTED_SONNET)
        assert result.allowed is True
        assert result.reason is None
        assert ADMITTED_SONNET in result.admitted_set
        assert ADMITTED_HAIKU in result.admitted_set
        assert result.decision_id == accepted_record.decision_id

    def test_accepted_rejects_provider_not_in_admitted_set(
        self, accepted_record: DecisionRecord
    ) -> None:
        result = check_enforcement(_ctx(accepted_record), "omniroute/opus")
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_DEGRADED_PROVIDER_NOT_ADMITTED

    def test_denied_excludes_provider_in_exclusions(self, denied_record: DecisionRecord) -> None:
        excluded = _first_exclusion(denied_record)
        result = check_enforcement(_ctx(denied_record), excluded)
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_DENIED_PROVIDER
        assert excluded in result.exclusions

    def test_denied_rejects_unknown_provider_too(self, denied_record: DecisionRecord) -> None:
        # The whole task was denied; an unknown actor is never optimistically admitted.
        result = check_enforcement(_ctx(denied_record), "omniroute/unknown")
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_DENIED_PROVIDER

    def test_degraded_admits_remaining_admitted_provider(
        self, degraded_record: DecisionRecord
    ) -> None:
        admitted = degraded_record.admitted[0].id
        result = check_enforcement(_ctx(degraded_record), admitted)
        assert result.allowed is True
        assert admitted in result.admitted_set
        assert ADMITTED_SONNET in result.exclusions

    def test_degraded_rejects_excluded_provider(self, degraded_record: DecisionRecord) -> None:
        excluded = _first_exclusion(degraded_record)
        result = check_enforcement(_ctx(degraded_record), excluded)
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_DENIED_PROVIDER

    def test_degraded_rejects_unadmitted_unexcluded_provider(
        self, degraded_record: DecisionRecord
    ) -> None:
        result = check_enforcement(_ctx(degraded_record), "omniroute/opus")
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_DEGRADED_PROVIDER_NOT_ADMITTED

    def test_degraded_with_none_actor_is_malformed(self, degraded_record: DecisionRecord) -> None:
        # degraded requires a concrete actor to disambiguate admitted vs excluded.
        result = check_enforcement(_ctx(degraded_record), None)
        assert result.allowed is False
        assert result.reason is EnforcementReason.DECISION_MALFORMED


# ---------------------------------------------------------------------------
# 3. Determinism (NFR-001) and invariants (NFR-003)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_inputs_produce_identical_results(
        self, accepted_record: DecisionRecord
    ) -> None:
        fixed_now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        a = check_enforcement(_ctx(accepted_record), ADMITTED_SONNET, now=fixed_now)
        b = check_enforcement(_ctx(accepted_record), ADMITTED_SONNET, now=fixed_now)
        assert a == b

    def test_allowed_result_with_reason_is_rejected(self) -> None:
        # Invariant: an allowed result must not carry a denial reason.
        with pytest.raises(ValueError):
            EnforcementResult(
                allowed=True,
                reason=EnforcementReason.DECISION_MISSING,
                decision_id="x",
                admitted_set=(),
                exclusions=(),
            )

    def test_denied_result_must_carry_reason(self) -> None:
        with pytest.raises(ValueError):
            EnforcementResult(
                allowed=False, reason=None, decision_id="x", admitted_set=(), exclusions=()
            )


class TestEnforcementContextInvariants:
    def test_expires_before_created_is_rejected(self, accepted_record: DecisionRecord) -> None:
        with pytest.raises(ValueError):
            EnforcementContext(
                decision_record=accepted_record,
                created_at=datetime(2026, 8, 20, tzinfo=UTC),
                expires_at=datetime(2026, 8, 19, tzinfo=UTC),
            )

    def test_naive_datetimes_are_coerced_to_utc(self, accepted_record: DecisionRecord) -> None:
        ctx = EnforcementContext(
            decision_record=accepted_record,
            created_at=datetime(2026, 8, 20),
            expires_at=datetime(2026, 8, 22),
        )
        assert ctx.created_at.tzinfo is not None
        assert ctx.expires_at is not None and ctx.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 4. Lifecycle verification gate (US2 / SC-002)
# ---------------------------------------------------------------------------


class TestEnforcementVerificationGate:
    def test_blocks_excluded_provider_step(self, degraded_record: DecisionRecord) -> None:
        excluded = _first_exclusion(degraded_record)
        gate = EnforcementVerificationGate()
        out = gate.evaluate(
            workflow_plan={"target_provider": excluded},
            stage="pre_dispatch",
            decision_record=degraded_record,
        )
        assert out["passed"] is False
        assert out["source"] == "enforcement_kernel"
        assert out["blocked_provider"] == excluded
        assert out["reason"] == EnforcementReason.DECISION_DENIED_PROVIDER.value
        assert out["decision_id"] == degraded_record.decision_id

    def test_admits_admitted_provider_step(self, degraded_record: DecisionRecord) -> None:
        admitted = degraded_record.admitted[0].id
        gate = EnforcementVerificationGate()
        out = gate.evaluate(
            workflow_plan={"target_provider": admitted},
            stage="pre_dispatch",
            decision_record=degraded_record,
        )
        assert out["passed"] is True
        assert out["blocked_provider"] is None
        assert admitted in out["admitted_set"]

    def test_missing_decision_record_fails_closed(self, degraded_record: DecisionRecord) -> None:
        gate = EnforcementVerificationGate()
        out = gate.evaluate(workflow_plan={"target_provider": ADMITTED_HAIKU}, stage="pre_dispatch")
        assert out["passed"] is False
        assert out["reason"] == EnforcementReason.DECISION_MISSING.value

    def test_accepts_context_over_decision_record(self, degraded_record: DecisionRecord) -> None:
        gate = EnforcementVerificationGate()
        ctx = _ctx(degraded_record)
        out = gate.evaluate(
            workflow_plan={"target_provider": degraded_record.admitted[0].id},
            stage="pre_dispatch",
            context=ctx,
        )
        assert out["passed"] is True

    def test_attribute_style_workflow_plan_supported(self, degraded_record: DecisionRecord) -> None:
        plan = type("Plan", (), {"target_provider": degraded_record.admitted[0].id})()
        gate = EnforcementVerificationGate()
        out = gate.evaluate(
            workflow_plan=plan, stage="pre_dispatch", decision_record=degraded_record
        )
        assert out["passed"] is True


# ---------------------------------------------------------------------------
# 5. Gateway dispatch guard (US3 / SC-003)
# ---------------------------------------------------------------------------


class TestDispatchWithEnforcement:
    def _attestation(self, provider: str | None) -> object:
        if provider is None:
            return type("A", (), {"resolved_route": None})()

        class _Route:
            pass

        resolved = type("Route", (), {"provider": provider})()
        return type("A", (), {"resolved_route": resolved})()

    def test_rejects_excluded_provider_before_dispatch(self, denied_record: DecisionRecord) -> None:
        excluded = _first_exclusion(denied_record)
        called = []

        with pytest.raises(EnforcementGatewayError) as exc:
            dispatch_with_enforcement(
                adapter=None,
                attestation=self._attestation(excluded),
                context=_ctx(denied_record),
                dispatch=lambda: called.append(1),
            )
        assert called == []  # no network call made
        assert exc.value.reason is EnforcementReason.DECISION_DENIED_PROVIDER
        assert exc.value.decision_id == denied_record.decision_id
        assert exc.value.actor_provider == excluded

    def test_allows_admitted_provider_then_dispatches(
        self, accepted_record: DecisionRecord
    ) -> None:
        result_payload = {"ok": True}
        out = dispatch_with_enforcement(
            adapter=None,
            attestation=self._attestation(ADMITTED_SONNET),
            context=_ctx(accepted_record),
            dispatch=lambda: result_payload,
        )
        assert out is result_payload

    def test_no_context_is_legacy_passthrough(self) -> None:
        result_payload = {"ok": True}
        out = dispatch_with_enforcement(
            adapter=None,
            attestation=self._attestation("anyone"),
            context=None,
            dispatch=lambda: result_payload,
        )
        assert out is result_payload

    def test_mapping_attestation_provider_supported(self, declined_record=None) -> None:
        from verdict.decision_kernel_demo import denied_decision as _denied

        excluded = _first_exclusion(_denied())
        with pytest.raises(EnforcementGatewayError):
            dispatch_with_enforcement(
                adapter=None,
                attestation={"provider": excluded},
                context=_ctx(_denied()),
                dispatch=lambda: None,
            )


# ---------------------------------------------------------------------------
# 6. SC-004 — enforcement decision_id traces to verify_decision (feature 003)
# ---------------------------------------------------------------------------


class TestAuditorTraceability:
    def test_decision_id_reproduces_via_verify_decision(
        self, accepted_record: DecisionRecord
    ) -> None:
        check_enforcement(_ctx(accepted_record), ADMITTED_SONNET)
        # verify_decision reproduces the receipt credential-free from inputs alone.
        assert (
            verify_decision(accepted_record, **demo_inputs()) is None
        )  # trusted receipt; mirrors 003's SC-001

    def test_enforcement_carries_authority_decision_id(self, denied_record: DecisionRecord) -> None:
        result = check_enforcement(_ctx(denied_record), _first_exclusion(denied_record))
        assert result.decision_id == denied_record.decision_id
        assert result.decision_id.startswith("sha256:")


# ---------------------------------------------------------------------------
# 7. NFR-004 — ≤1 ms p99 microbenchmark
# ---------------------------------------------------------------------------


class TestEnforcementPerformance:
    def test_p99_under_one_millisecond(self, accepted_record: DecisionRecord) -> None:
        ctx = _ctx(accepted_record)
        durations: list[float] = []
        for _ in range(1000):
            start = datetime.now(UTC)
            check_enforcement(ctx, ADMITTED_SONNET)
            durations.append((datetime.now(UTC) - start).total_seconds() * 1000.0)
        durations.sort()
        p99 = durations[int(len(durations) * 0.99)]
        assert p99 < 1.0, (
            f"p99={p99:.4f}ms exceeds 1ms budget; median={statistics.median(durations):.4f}ms"
        )
