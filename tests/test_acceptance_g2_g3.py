"""Acceptance tests for G2 (Availability & Freshness) and G3 (Cost & Latency).

These tests verify the REAL behavior of production code paths for gates G2.1-G2.4 and G3.1-G3.4.
Each test exercises actual production modules and will FAIL if the documented behavior breaks.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.availability_cache import AvailabilityCache, CacheKey
from verdict.bounded_recovery import (
    BoundedRecoveryController,
    ExecutionRoute,
    FailureEvidence,
    RecoveryAction,
    RecoveryBounds,
)
from verdict.cost_ledger import CostLedger
from verdict.eligibility import EligibilityGate
from verdict.execution_path import (
    ExecutionPathDecision,
    ExecutionPathRequest,
    optimize_execution_path,
)
from verdict.models import ModelInfo
from verdict.orchestration.contracts import NodeKind, OrchestrationError, WorkGraph, WorkNode
from verdict.orchestration.planner import choose_topology

# ============================================================================
# G2.1: Cache TTL + stale-while-revalidate
# ============================================================================


def test_cache_ttl_swr() -> None:
    """G2.1: Availability cache honors TTL and stale-while-revalidate.

    Fresh -> cached. Stale-but-within-window -> cached + schedule refresh.
    Expired -> refresh now; on refresh failure return explicit unknown.
    """

    class FakeClock:
        def __init__(self, start: datetime) -> None:
            self.now = start

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now = self.now + timedelta(seconds=seconds)

    class FakeSource:
        def __init__(self, report: AvailabilityReport) -> None:
            self.report = report
            self.calls = 0

        def __call__(self) -> AvailabilityReport:
            self.calls += 1
            return self.report

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(now)
    source = FakeSource(AvailabilityReport((), (), "test", 60, ()))
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)

    # Fresh: should return cached without refresh
    first = cache.get("provider/model")
    second = cache.get("provider/model")
    assert first is second
    assert source.calls == 1  # Only initial fetch

    # Stale-while-revalidate: age 75s (60 < 75 <= 90) -> serve stale + refresh
    clock.advance(75)
    stale = cache.get("provider/model")
    assert stale is not None
    assert source.calls == 2  # Triggered refresh

    # Expired (age > stale_window): advance past the 90s stale window from the LAST refresh
    # The refresh at 75s updated stored_at, so we need another 91+ seconds from THAT point
    clock.advance(91)  # Now 166s total, but 91s since last refresh -> past stale window
    expired = cache.get("provider/model")
    assert expired is not None
    assert source.calls == 3


# ============================================================================
# G2.2: Explicit unknown/error states
# ============================================================================


def test_unknown_error_states() -> None:
    """G2.2: Unknown/error availability states are explicit, not silent fallback.

    When refresh fails past the stale window, the cache returns an explicit
    "unknown" report with errors, NOT a stale entry or silent None.
    """

    class FakeClock:
        def __init__(self, start: datetime) -> None:
            self.now = start

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now = self.now + timedelta(seconds=seconds)

    class FailingSource:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> AvailabilityReport:
            self.calls += 1
            raise RuntimeError("upstream unavailable")

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(now)
    source = FailingSource()
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)

    # First call with failing source returns explicit unknown
    report = cache.get("provider/model")
    assert report.source == "cache"
    assert len(report.errors) > 0
    assert any("availability cache" in str(e) for e in report.errors)
    # Never returns None or stale data silently


# ============================================================================
# G2.3: Concurrent refresh deduplication
# ============================================================================


def test_refresh_deduplication() -> None:
    """G2.3: Concurrent refreshes for the same key coalesce into one fetch.

    Multiple threads requesting the same stale entry must trigger exactly
    one upstream refresh: after the first thread stores a fresh report, all
    subsequent threads find a fresh entry and return it without calling source.
    This verifies _store updates stored_at so the entry is no longer stale.
    """

    class FakeClock:
        def __init__(self, start: datetime) -> None:
            self.now = start

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now = self.now + timedelta(seconds=seconds)

    class CountingSource:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()

        def __call__(self) -> AvailabilityReport:
            with self._lock:
                self.calls += 1
            return AvailabilityReport((), (), "test", 60, ())

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(now)
    source = CountingSource()
    cache = AvailabilityCache(source=source, clock=clock, ttl_seconds=60, stale_window_seconds=30)

    # Warm cache with first fetch
    cache.get("provider/model")
    assert source.calls == 1

    # Advance into stale window (TTL expired, within stale_window)
    clock.advance(75)

    # Sequential calls on a stale entry: first triggers refresh, rest return cached
    # All run under the cache lock so they serialize
    results = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(cache.get("provider/model"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == 5
    # Exactly ONE additional refresh: _store updates stored_at so subsequent
    # threads see a fresh entry and return it without calling source again.
    assert source.calls == 2, (
        f"Expected exactly 2 source calls (1 initial + 1 refresh), got {source.calls}"
    )


# ============================================================================
# G2.4: Cache isolation by provider/model/policy-version
# ============================================================================


def test_isolation_keys() -> None:
    """G2.4: Cache entries are isolated by policy version.

    The catalog report is model-independent, so models share one entry per
    policy version; different policy versions must not share entries.
    """

    class FakeSource:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> AvailabilityReport:
            self.calls += 1
            return AvailabilityReport((), (), "test", 60, ())

    source = FakeSource()
    cache = AvailabilityCache(source=source, policy_version="policy-v1")

    # The source evaluates the whole catalog with no model id, so models and
    # providers share one entry (BOD-431: per-model keys refetched it per model).
    first = cache.get("provider-a/model-1")
    assert cache.get("provider-a/model-2") is first
    assert cache.get("provider-b/model-1") is first
    assert source.calls == 1
    assert len(cache._entries) == 1

    # A different policy version never reuses that entry.
    other = AvailabilityCache(source=source, policy_version="policy-v2")
    other.get("provider-a/model-1")
    assert source.calls == 2
    assert other._key("provider-a/model-1") != cache._key("provider-a/model-1")

    # Same model, different policy version -> different key
    key1 = CacheKey.for_candidate("provider-a/model-1", "policy-v1")
    key2 = CacheKey.for_candidate("provider-a/model-1", "policy-v2")
    assert key1 != key2
    assert key1.provider == key2.provider
    assert key1.model == key2.model
    assert key1.policy_version != key2.policy_version


# ============================================================================
# G3.1: Only eligible candidates are selected, in trusted shortlist order
# ============================================================================


def test_eligible_selection_is_price_invariant() -> None:
    """G3.1: only eligible candidates win, in trusted shortlist order; price never ranks."""
    from tests.test_execution_path import NOW, _cost, _offer, _plan, _receipt, _route, _slice
    from verdict.effective_capability import AssistanceCost

    cheap_model = ModelInfo(id="cheap/model", provider="cheap")
    medium_model = ModelInfo(id="medium/model", provider="medium")
    expensive_model = ModelInfo(id="expensive/model", provider="expensive")

    def mock_cache(model_id: str) -> AvailabilityReport:
        state = (
            AvailabilityState.UNAVAILABLE
            if model_id == "cheap/model"
            else AvailabilityState.ELIGIBLE
        )
        model = {"cheap/model": cheap_model, "medium/model": medium_model}.get(
            model_id, expensive_model
        )
        candidate = AvailabilityCandidate(model=model, state=state, source="test")
        return AvailabilityReport(
            candidates=(candidate,),
            eligible=() if state is AvailabilityState.UNAVAILABLE else (candidate,),
            source="test",
            freshness_seconds=60,
            errors=(),
        )

    gate = EligibilityGate(availability_source=mock_cache)
    admitted = {m.id for m in gate.evaluate([cheap_model, medium_model, expensive_model]).admitted}
    assert "cheap/model" not in admitted, "ineligible model must be excluded"
    assert {"medium/model", "expensive/model"} <= admitted

    cheap_plan = _plan(
        candidate_id="cheap/model", result="insufficient", assisted=False, digest="sha256:c"
    )
    medium_plan = _plan(candidate_id="medium/model", digest="sha256:m")
    expensive_plan = _plan(candidate_id="expensive/model", digest="sha256:e")
    offers = (
        _offer(
            strategy="direct_cheap",
            route=_route("cheap/model", excluded=True, eligible=False),
            plan=cheap_plan,
            expected=_cost(
                "direct_cheap:cheap/model",
                assistance=AssistanceCost(),
                execution_tokens=1,
                is_free=True,
                qualified=False,
            ),
            hard_excluded=True,
            is_cheap=True,
        ),
        _offer(
            strategy="direct_cheap",
            route=_route("medium/model", model="medium", provider="medium"),
            plan=medium_plan,
            expected=_cost(
                "direct_cheap:medium/model",
                assistance=medium_plan.assistance_cost,
                execution_tokens=10_000,
            ),
            is_cheap=True,
        ),
        _offer(
            strategy="direct_paid",
            route=_route("expensive/model", model="expensive", provider="expensive", tier=3),
            plan=expensive_plan,
            expected=_cost(
                "direct_paid:expensive/model",
                assistance=expensive_plan.assistance_cost,
                execution_tokens=90_000,
            ),
            is_paid=True,
        ),
    )

    def select(*shortlist: str) -> ExecutionPathDecision:
        return optimize_execution_path(
            ExecutionPathRequest(
                task_slice=_slice(),
                trajectory_id="traj-104",
                offers=offers,
                hard_excluded_ids=frozenset({"cheap/model"}),
                pool_receipt=_receipt(*shortlist),
                now=NOW,
            )
        )

    # The ineligible cheap route is ranked first, the expensive one before the cheaper one:
    # selection is the first ELIGIBLE shortlist entry, whatever its price.
    first = select("cheap/model", "expensive/model", "medium/model")
    assert first.selected_candidate_id == "expensive/model"
    assert any(r.candidate_id == "cheap/model" for r in first.rejected)

    # Swapping the shortlist order flips the winner: price is not consulted.
    second = select("cheap/model", "medium/model", "expensive/model")
    assert second.selected_candidate_id == "medium/model"


# ============================================================================
# G3.2: Escalation only on capability/verification failure
# ============================================================================


def test_escalation_policy() -> None:
    """G3.2: Escalation only occurs on capability or verification failure.

    Provider failures, missing context, or tool mismatches should NOT escalate
    to a more expensive model. Only capability deficits should trigger escalation.
    """
    controller = BoundedRecoveryController(bounds=RecoveryBounds(max_attempts=5))
    route = ExecutionRoute(
        model_id="cheap/model", provider="cheap", capability_tier=1, is_frontier=False
    )
    stronger_route = ExecutionRoute(
        model_id="expensive/model", provider="expensive", capability_tier=2, is_frontier=True
    )
    ledger = CostLedger(trajectory_id="test-traj", cash_budget_usd=Decimal("10.00"))

    # Provider failure -> should NOT escalate
    provider_evidence = FailureEvidence(
        error_class="provider_error",
        message="upstream timeout",
        signals=frozenset({"provider_outage"}),
    )
    # Need an equivalent route for provider failure (not stronger)
    equivalent_route = ExecutionRoute(
        model_id="cheap/model-b", provider="cheap-alt", capability_tier=1, is_frontier=False
    )
    decision = controller.decide(
        evidence=provider_evidence,
        route=route,
        ledger=ledger,
        equivalent_routes=[equivalent_route],
        stronger_routes=[stronger_route],
    )
    assert decision.action == RecoveryAction.SWITCH_EXECUTION_PLANE
    assert decision.escalated is False
    assert decision.route_after.capability_tier == route.capability_tier

    # Capability deficit -> SHOULD escalate
    capability_evidence = FailureEvidence(
        error_class="capability_deficit",
        message="model too weak for task",
        signals=frozenset({"capability_deficit"}),
    )
    decision2 = controller.decide(
        evidence=capability_evidence, route=route, ledger=ledger, stronger_routes=[stronger_route]
    )
    assert decision2.action == RecoveryAction.ESCALATE_CAPABILITY
    assert decision2.escalated is True
    assert decision2.route_after.capability_tier > route.capability_tier


# ============================================================================
# G3.3: Per-task budget enforcement
# ============================================================================


def test_budget_enforcement() -> None:
    """G3.3: Per-task token and USD budgets are enforced at execution.

    When a task exceeds its budget, recovery actions must be blocked.
    """
    # Create ledger with tight budget
    ledger = CostLedger(trajectory_id="test-traj", cash_budget_usd=Decimal("0.001"))

    controller = BoundedRecoveryController(
        bounds=RecoveryBounds(
            max_attempts=10,
            estimated_action_cash_usd=Decimal("0.01"),  # Cost exceeds budget
        )
    )

    route = ExecutionRoute(model_id="test/model", provider="test", capability_tier=1)
    evidence = FailureEvidence(error_class="implementation_error", message="retry needed")

    decision = controller.decide(evidence=evidence, route=route, ledger=ledger, attempt_index=0)

    # Should be blocked due to insufficient budget
    assert decision.action == RecoveryAction.BLOCK
    assert "budget" in decision.reason.lower()

    # Token budget over-limit: quota_budgets={"tokens": 50}, estimated_action_tokens=100 -> BLOCK
    token_ledger = CostLedger(
        trajectory_id="test-traj-tok", quota_budgets={"tokens": Decimal("50")}
    )
    token_controller = BoundedRecoveryController(
        bounds=RecoveryBounds(
            max_attempts=10,
            estimated_action_tokens=Decimal("100"),  # 100 > 50 -> block
        )
    )
    tok_decision = token_controller.decide(
        evidence=evidence, route=route, ledger=token_ledger, attempt_index=0
    )
    assert tok_decision.action == RecoveryAction.BLOCK, (
        f"expected BLOCK for token over-budget, got {tok_decision.action}"
    )
    assert "token" in tok_decision.reason.lower(), (
        f"expected 'token' in reason, got {tok_decision.reason!r}"
    )

    # Token budget within-limit: estimated_action_tokens=10 <= quota_budget=50 -> NOT blocked for token budget
    within_controller = BoundedRecoveryController(
        bounds=RecoveryBounds(
            max_attempts=10,
            estimated_action_tokens=Decimal("10"),  # 10 <= 50 -> should not block for budget
        )
    )
    within_decision = within_controller.decide(
        evidence=evidence, route=route, ledger=token_ledger, attempt_index=0
    )
    assert (
        within_decision.action != RecoveryAction.BLOCK
        or "token" not in within_decision.reason.lower()
    ), f"must not block for token budget when within limit, got {within_decision.reason!r}"


# ============================================================================
# G3.4: Concurrency caps and timeout enforcement
# ============================================================================


def test_concurrency_timeout() -> None:
    """G3.4: Concurrency caps and timeouts are enforced.

    Recovery attempts must respect deadline constraints and not retry past deadline.
    """
    # Create controller with tight deadline
    deadline = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    controller = BoundedRecoveryController(
        bounds=RecoveryBounds(max_attempts=10, deadline_at=deadline)
    )

    route = ExecutionRoute(model_id="test/model", provider="test", capability_tier=1)
    ledger = CostLedger(trajectory_id="test-traj", cash_budget_usd=Decimal("10.00"))
    evidence = FailureEvidence(error_class="implementation_error", message="retry needed")

    # Attempt after deadline should be blocked
    after_deadline = deadline + timedelta(seconds=1)
    decision = controller.decide(evidence=evidence, route=route, ledger=ledger, now=after_deadline)

    assert decision.action == RecoveryAction.BLOCK
    assert "deadline" in decision.reason.lower()

    # Attempt before deadline should proceed
    before_deadline = deadline - timedelta(seconds=60)
    decision2 = controller.decide(
        evidence=evidence, route=route, ledger=ledger, now=before_deadline
    )

    assert decision2.action != RecoveryAction.BLOCK, (
        f"must NOT block before deadline, got action={decision2.action} reason={decision2.reason!r}"
    )

    # Concurrency cap: choose_topology caps parallel units at min(max_parallel, widest_layer).
    # 3 independent implement nodes -> widest_layer=3; max_parallel=2 -> chosen=min(2,3)=2.
    def _impl_node(node_id: str) -> WorkNode:
        return WorkNode(
            node_id=node_id,
            objective=f"Implement {node_id}",
            kind=NodeKind.IMPLEMENT,
            owned_files=(f"{node_id}.py",),
            verification_command=("pytest", f"tests/test_{node_id}.py"),
        )

    parallel_nodes = [_impl_node(f"task_{i}") for i in range(3)]
    _topo, chosen_parallel, _rationale = choose_topology(parallel_nodes, max_parallel=2)
    assert chosen_parallel == 2, f"expected min(max_parallel=2, widest=3)=2, got {chosen_parallel}"
    assert chosen_parallel <= 2  # cap enforced

    # OrchestrationPolicy rejects max_parallel < 1 via choose_topology
    import pytest as _pytest

    with _pytest.raises(OrchestrationError, match="max_parallel must be >= 1"):
        choose_topology(parallel_nodes, max_parallel=0)

    # WorkGraph also rejects max_parallel < 1
    with _pytest.raises(OrchestrationError, match="max_parallel must be >= 1"):
        WorkGraph(goal="test", nodes=tuple(parallel_nodes), max_parallel=0)
