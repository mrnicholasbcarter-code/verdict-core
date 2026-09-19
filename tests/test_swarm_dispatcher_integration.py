"""
Integration tests for swarm dispatcher with swarm contracts (Issue #44 / Slice 37.2).

BOD-127: swarm binds an already-authorized selected_route; it does not invent
least-cost assignment. Envelope filtering and fan-out remain execution gates.
"""

from __future__ import annotations

from datetime import datetime, timezone

from verdict.contracts import AvailabilitySnapshot, RuntimeCandidate
from verdict.session_economics import ConcreteRoute
from verdict.swarm_contracts import (
    SwarmTaskBudget,
    SwarmTaskResult,
    TerminationReason,
    build_swarm_task_envelope,
)
from verdict.swarm_dispatcher import dispatch_swarm_task


class TestSwarmDispatcherIntegration:
    """Integration tests for swarm dispatcher with swarm contracts."""

    NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    def _candidate(self, runtime_id: str, *, cost: float = 1, **kwargs) -> RuntimeCandidate:
        return RuntimeCandidate(
            runtime_id=runtime_id,
            catalog_present=kwargs.pop("catalog_present", True),
            live_eligible=kwargs.pop("live_eligible", True),
            availability=kwargs.pop("availability", "ready"),
            signals={"cost_usd": {"value": cost}, **kwargs.pop("signals", {})},
            capabilities=list(kwargs.pop("capabilities", [])),
            model=runtime_id,
        )

    def _snapshot(
        self, *candidates: RuntimeCandidate, state: str = "ready", ttl_seconds: int = 60
    ) -> AvailabilitySnapshot:
        return AvailabilitySnapshot(
            observed_at=self.NOW.isoformat(),
            state=state,
            ttl_seconds=ttl_seconds,
            candidates=list(candidates),
        )

    def _route(self, model: str) -> ConcreteRoute:
        return ConcreteRoute(
            route_id=model,
            gateway="g",
            provider="p",
            model=model,
            credential_pool="pool",
            capability_tier=1,
            eligible=True,
            excluded=False,
        )

    def test_missing_selected_route_fails_closed(self):
        envelope = build_swarm_task_envelope(
            objective="Implement feature X", required_capabilities=["coding"]
        )
        snap = self._snapshot(self._candidate("coder-1", cost=0.1, capabilities=["coding"]))
        result = dispatch_swarm_task(envelope, snap, now=self.NOW)
        assert result.selected is None
        assert result.reason == "missing_authorized_selected_route"

    def test_swarm_envelope_eligibility_filters_candidates(self):
        """Swarm envelope eligibility gates candidates before binding selected_route."""
        envelope = build_swarm_task_envelope(
            objective="Implement feature X",
            allowed_paths=["/home/nick/dev/project"],
            budget=SwarmTaskBudget(max_usd=0.50, max_tokens=10000, max_latency_ms=30000),
            required_capabilities=["coding"],
            model_floor="auto/best-coding",
        )

        candidate_eligible = self._candidate("coder-1", cost=0.1, capabilities=["coding"])
        candidate_ineligible = self._candidate("chat-1", cost=0.05, capabilities=["chat"])

        snap = self._snapshot(candidate_eligible, candidate_ineligible)
        result = dispatch_swarm_task(
            envelope, snap, now=self.NOW, selected_route=self._route("coder-1")
        )

        assert result.selected is not None
        assert result.selected.runtime_id == "coder-1"
        assert result.reason == "selected_route"

    def test_authorized_route_not_cheapest_invention(self):
        """Authorized selected_route wins even when a cheaper candidate exists."""
        envelope = build_swarm_task_envelope(
            objective="Code task",
            budget=SwarmTaskBudget(max_usd=1.0, max_tokens=5000),
            required_capabilities=["coding"],
        )

        cheap = self._candidate("cheap-coder", cost=0.05, capabilities=["coding"])
        expensive = self._candidate("expensive-coder", cost=0.20, capabilities=["coding"])

        snap = self._snapshot(cheap, expensive)
        result = dispatch_swarm_task(
            envelope, snap, now=self.NOW, selected_route=self._route("expensive-coder")
        )

        assert result.selected is not None
        assert result.selected.runtime_id == "expensive-coder"

    def test_budget_exceeded_returns_no_selection(self):
        """All candidates over budget returns no selection with proper reason."""
        envelope = build_swarm_task_envelope(
            objective="Code task",
            budget=SwarmTaskBudget(max_usd=0.05, max_tokens=5000),
            required_capabilities=["coding"],
        )

        expensive1 = self._candidate("expensive-1", cost=0.10, capabilities=["coding"])
        expensive2 = self._candidate("expensive-2", cost=0.15, capabilities=["coding"])

        snap = self._snapshot(expensive1, expensive2)
        result = dispatch_swarm_task(
            envelope, snap, now=self.NOW, selected_route=self._route("expensive-1")
        )

        assert result.selected is None
        assert any("budget" in r.lower() for item in result.explanations for r in item.reasons) or (
            "envelope" in result.reason.lower() or "eligible" in result.reason.lower()
        )

    def test_capability_matching_required(self):
        """Required capabilities are mandatory for the authorized route."""
        envelope = build_swarm_task_envelope(
            objective="Full stack task",
            required_capabilities=["coding", "testing"],
            optional_capabilities=["documentation"],
        )

        full = self._candidate(
            "full-stack", cost=0.1, capabilities=["coding", "testing", "documentation"]
        )
        missing_req = self._candidate("doc-only", cost=0.02, capabilities=["documentation"])

        snap = self._snapshot(full, missing_req)
        result = dispatch_swarm_task(
            envelope, snap, now=self.NOW, selected_route=self._route("full-stack")
        )
        assert result.selected is not None
        assert result.selected.runtime_id == "full-stack"

    def test_bounded_fan_out_max_parallelism(self):
        """Fan-out respects max_parallelism from envelope for authorized bind."""
        envelope = build_swarm_task_envelope(objective="Parallel task", max_parallelism=2)

        candidates = [self._candidate(f"worker-{i}", cost=0.05) for i in range(5)]
        snap = self._snapshot(*candidates)
        result = dispatch_swarm_task(
            envelope, snap, now=self.NOW, selected_route=self._route("worker-2")
        )
        assert result.selected is not None
        assert result.selected.runtime_id == "worker-2"

    def test_stop_conditions_enforced(self):
        """Stop conditions from envelope are enforced."""
        envelope = build_swarm_task_envelope(
            objective="Long task",
            budget=SwarmTaskBudget(max_usd=0.01),
            stop_conditions=["budget_exceeded", "timeout", "max_iterations"],
        )

        candidates = [self._candidate(f"worker-{i}", cost=0.10) for i in range(3)]
        snap = self._snapshot(*candidates)
        result = dispatch_swarm_task(
            envelope, snap, now=self.NOW, selected_route=self._route("worker-0")
        )

        assert result.selected is None
        assert any("budget" in r.lower() for item in result.explanations for r in item.reasons) or (
            "envelope" in result.reason.lower() or "eligible" in result.reason.lower()
        )


class TestSwarmTaskLifecycle:
    """Tests for full task lifecycle with attempts and verification."""

    def test_attempt_creation_and_completion(self):
        """Task attempt lifecycle: created -> running -> completed."""
        from verdict.swarm_contracts import create_task_attempt

        attempt = create_task_attempt("task-123")
        assert attempt.task_id == "task-123"
        assert attempt.state == "pending"

        completed = attempt.mark_completed(
            result=SwarmTaskResult.SUCCESS,
            reason=TerminationReason.COMPLETED,
            output_refs=["artifact-1"],
        )
        assert completed.state == "completed"
        assert completed.result == SwarmTaskResult.SUCCESS
        assert completed.termination_reason == TerminationReason.COMPLETED
        assert completed.output_artifact_refs == ["artifact-1"]

    def test_verification_passes_fails(self):
        """Verification result tracking."""
        from verdict.swarm_contracts import SwarmTaskVerification

        passed = SwarmTaskVerification(
            task_id="task-1",
            attempt_id="attempt-1",
            passed=True,
            checks={"lint": True, "tests": True},
        )
        assert passed.passed is True

        failed = SwarmTaskVerification(
            task_id="task-1",
            attempt_id="attempt-1",
            passed=False,
            checks={"lint": True, "tests": False},
            details={"tests": "2 failed"},
        )
        assert failed.passed is False
        assert failed.checks["tests"] is False
