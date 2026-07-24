"""
Bounded fan-out, backpressure, and lower-tier iteration loop (Issue #45 / Slice 37.3).

This module extends the swarm dispatcher with:
- Bounded fan-out limiting concurrent task assignments
- Backpressure mechanism for queue depth and timeout handling
- Lower-tier iteration loop with escalation support
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from verdict.contracts import AvailabilitySnapshot, RuntimeCandidate
from verdict.dispatcher import (
    DispatchPolicy,
    DispatchResult,
)
from verdict.dispatcher import (
    SwarmDispatcher as BaseSwarmDispatcher,
)
from verdict.models import ModelInfo
from verdict.router import select_best_model
from verdict.swarm_contracts import (
    SwarmTaskEnvelope,
)


@dataclass
class FanOutLimiter:
    """
    Limits concurrent task fan-out with backpressure.

    Implements token bucket for concurrency control and
    queue depth monitoring for backpressure.
    """
    max_concurrent: int = 1
    max_queue_depth: int = 100
    backpressure_timeout: float = 30.0  # seconds

    # Runtime state
    _active_count: int = 0
    _queue: list[asyncio.Future[bool]] = field(default_factory=list)
    _waiting: list[asyncio.Future[bool]] = field(default_factory=list)

    def try_acquire(self) -> bool:
        """Try to acquire a fan-out slot. Returns True if acquired."""
        if self._active_count < self.max_concurrent:
            self._active_count += 1
            return True
        return False

    def release(self) -> None:
        """Release a fan-out slot."""
        self._active_count = max(0, self._active_count - 1)
        self._process_queue()

    def _process_queue(self) -> None:
        """Process waiting tasks if slots available."""
        while self._queue and self._active_count < self.max_concurrent:
            task = self._queue.pop(0)
            self._active_count += 1
            task.set_result(True)

    def enqueue(self) -> asyncio.Future[bool]:
        """Enqueue a task waiting for fan-out slot."""
        if len(self._queue) >= self.max_queue_depth:
            raise RuntimeError(f"Queue depth exceeded: {self.max_queue_depth}")

        future = asyncio.get_event_loop().create_future()
        self._queue.append(future)
        return future

    def current_load(self) -> float:
        """Current load as fraction of max (0.0 to 1.0)."""
        return self._active_count / self.max_concurrent if self.max_concurrent > 0 else 0.0

    def is_backpressured(self) -> bool:
        """Check if system is under backpressure."""
        return self._active_count >= self.max_concurrent or len(self._queue) >= self.max_queue_depth * 0.8


@dataclass
class IterationState:
    """Tracks state of a lower-tier iteration loop."""
    attempt: int = 0
    max_attempts: int = 3
    last_result: DispatchResult | None = None
    escalation_depth: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_elapsed: float = 0.0


class SwarmDispatcher:
    """
    Swarm-aware dispatcher with bounded fan-out, backpressure, and iteration loop.

    Extends base dispatcher with:
    - Envelope-based eligibility filtering
    - Budget-aware candidate selection
    - Capability matching (required vs optional)
    - Stop condition enforcement
    - Fan-out limiting and backpressure
    - Lower-tier iteration loop with escalation
    """

    def __init__(
        self,
        policy: SwarmDispatchPolicy | None = None,
        fan_out_limiter: FanOutLimiter | None = None,
    ) -> None:
        if policy is None:
            policy = SwarmDispatchPolicy()
        self.policy = policy

        # Fan-out and backpressure
        self.fan_out = fan_out_limiter or FanOutLimiter(
            max_concurrent=policy.max_concurrency,
            max_queue_depth=100,
            backpressure_timeout=policy.timeout_seconds,
        )

        # Iteration state
        self._iteration_state: dict[str, IterationState] = {}

        # Create base dispatcher
        self._base_dispatcher = BaseSwarmDispatcher(self.policy.base_policy)

    def _get_candidate_cost(self, candidate: RuntimeCandidate) -> float | None:
        """Extract cost from candidate signals."""
        cost_signal = candidate.signals.get("cost_usd")
        if isinstance(cost_signal, dict):
            return cost_signal.get("value")
        return None

    def _filter_by_envelope(
        self,
        candidates: list[RuntimeCandidate],
        envelope: Any,
    ) -> list[RuntimeCandidate]:
        """Filter candidates by swarm envelope eligibility rules."""
        filtered = []

        for candidate in candidates:
            # Check required capabilities
            if envelope.required_capabilities:
                candidate_caps = set(candidate.capabilities or [])
                required = set(envelope.required_capabilities)
                if not required.issubset(candidate_caps):
                    continue  # Missing required capability

            # Check budget
            if envelope.budget is not None:
                cost = self._get_candidate_cost(candidate)
                if cost is not None and cost > envelope.budget.max_usd:
                    continue  # Over budget

            filtered.append(candidate)

        return filtered

    def dispatch(
        self,
        snapshot: AvailabilitySnapshot,
        now: datetime | None = None,
        task_id: str | None = None,
    ) -> Any:
        """
        Dispatch with envelope-aware filtering, fan-out limiting, and iteration loop.

        Args:
            snapshot: Availability snapshot with candidates
            now: Optional timestamp for deterministic results
            task_id: Optional task ID for iteration tracking

        Returns:
            DispatchResult with selected candidate or None
        """
        envelope = getattr(self.policy, "envelope", None)

        # Check fan-out availability
        if not self.fan_out.try_acquire() and self.fan_out.is_backpressured():
            return DispatchResult(
                    selected=None,
                    explanations=(),
                    eligible=(),
                    dry_run=True,
                    reason="backpressure: fan-out limit reached",
                    estimated_cost=0.0,
                    escalation_depth=0,
                )
            # Could await enqueue here for async version

        try:
            # Use base dispatcher for initial selection
            result = self._base_dispatcher.dispatch(snapshot, now=now)

            if envelope is None:
                return result  # No envelope, use base behavior

            # Filter eligible candidates by envelope rules
            eligible = list(result.eligible) if result.eligible else []
            filtered = self._filter_by_envelope(eligible, envelope)

            if not filtered:
                return DispatchResult(
                    selected=None,
                    explanations=result.explanations,
                    eligible=tuple(),
                    dry_run=True,
                    reason="no eligible candidates after envelope filtering",
                    estimated_cost=0.0,
                    escalation_depth=0,
                )

            # Re-select from filtered candidates (least cost)
            model_infos = []
            for c in filtered:
                cost = self._get_candidate_cost(c)
                model_info = ModelInfo(
                    id=c.runtime_id,
                    provider=c.provider or "unknown",
                    capability_tier=1,
                    capabilities=frozenset(c.capabilities or []),
                )
                model_infos.append(model_info)

            best_model, _ = select_best_model(
                candidates=model_infos,
                tier=0,
                configs={},
            )

            # Find the matching RuntimeCandidate
            selected = None
            if best_model:
                for c in filtered:
                    if c.runtime_id == best_model.id:
                        selected = c
                        break

            if selected is None:
                selected = min(filtered, key=lambda c: self._get_candidate_cost(c) or float("inf"))

            cost = self._get_candidate_cost(selected) or 0.0
            return DispatchResult(
                selected=selected,
                explanations=result.explanations,
                eligible=tuple(filtered),
                dry_run=True,
                reason="selected",
                estimated_cost=cost,
                escalation_depth=0,
            )

        finally:
            self.fan_out.release()

    async def dispatch_async(
        self,
        snapshot: AvailabilitySnapshot,
        now: datetime | None = None,
        task_id: str | None = None,
    ) -> Any:
        """
        Async dispatch with fan-out queue and backpressure handling.

        Waits for fan-out slot if under backpressure.
        """
        # Try to acquire fan-out slot
        if not self.fan_out.try_acquire():
            # Wait for slot with timeout
            try:
                future = self.fan_out.enqueue()
                await asyncio.wait_for(future, timeout=self.fan_out.backpressure_timeout)
            except (asyncio.TimeoutError, RuntimeError) as e:
                return DispatchResult(
                    selected=None,
                    explanations=(),
                    eligible=(),
                    dry_run=True,
                    reason=f"backpressure timeout: {e}",
                    estimated_cost=0.0,
                    escalation_depth=0,
                )

        try:
            return self.dispatch(snapshot, now, task_id)
        finally:
            self.fan_out.release()

    def iterate_lower_tier(
        self,
        snapshot: AvailabilitySnapshot,
        task_id: str,
        now: datetime | None = None,
        max_escalation_depth: int | None = None,
    ) -> Any:
        """
        Lower-tier iteration loop with escalation.

        Implements the iteration loop:
        1. Try current tier candidates
        2. On failure/budget exceed, escalate to next tier
        3. Track attempts and enforce max attempts
        4. Return final result after max escalation
        """

        envelope = getattr(self.policy, "envelope", None)
        policy = self.policy.base_policy

        if max_escalation_depth is None:
            max_escalation_depth = policy.max_escalation_depth

        # Get or create iteration state
        state = self._iteration_state.get(task_id)
        if state is None:
            state = IterationState()
            self._iteration_state[task_id] = state

        state.attempt += 1
        start = time.time()

        # Check if we've exceeded max attempts
        if state.attempt > state.max_attempts:
            return DispatchResult(
                selected=None,
                explanations=(),
                eligible=(),
                dry_run=True,
                reason=f"max attempts ({state.max_attempts}) exceeded",
                estimated_cost=0.0,
                escalation_depth=state.escalation_depth,
            )

        # Check escalation depth
        if state.escalation_depth >= max_escalation_depth:
            return DispatchResult(
                selected=None,
                explanations=(),
                eligible=(),
                dry_run=True,
                reason=f"max escalation depth ({max_escalation_depth}) reached",
                estimated_cost=0.0,
                escalation_depth=state.escalation_depth,
            )

        # Create modified snapshot for current tier
        # (simplified: just dispatch with current policy)
        result = self.dispatch(snapshot, now, task_id)

        state.last_result = result
        state.last_elapsed = time.time() - start

        # Check if we should escalate
        should_escalate = (
            result.selected is None
            or (envelope and envelope.budget and result.estimated_cost > envelope.budget.max_usd)
            or state.last_elapsed > policy.timeout_seconds
        )

        if should_escalate and state.escalation_depth < max_escalation_depth:
            # Escalate to next tier
            state.escalation_depth += 1
            return self.iterate_lower_tier(snapshot, task_id, now, max_escalation_depth)

        return result

    def reset_iteration(self, task_id: str) -> None:
        """Reset iteration state for a task."""
        self._iteration_state.pop(task_id, None)


@dataclass
class SwarmDispatchPolicy:
    """
    Extended dispatch policy that incorporates swarm task envelope.

    Adds budget, capability, and stop-condition awareness to base dispatcher.
    """
    # Base policy
    base_policy: DispatchPolicy = field(default_factory=DispatchPolicy)

    # Swarm envelope reference
    envelope: Any = None  # SwarmTaskEnvelope

    # Budget enforcement
    enforce_budget: bool = True
    enforce_capabilities: bool = True
    enforce_stop_conditions: bool = True

    def __post_init__(self) -> None:
        if self.envelope is not None:
            # Derive policy from envelope if not explicitly set
            if self.base_policy.max_budget is None and self.envelope.budget is not None:
                object.__setattr__(self.base_policy, "max_budget", self.envelope.budget.max_usd)
            if self.base_policy.required_capabilities is None and self.envelope.required_capabilities:
                object.__setattr__(self.base_policy, "required_capabilities", frozenset(self.envelope.required_capabilities))
            if self.base_policy.max_concurrency == 1 and self.envelope.max_parallelism:
                object.__setattr__(self.base_policy, "max_concurrency", self.envelope.max_parallelism)
            if self.base_policy.timeout_seconds == 30.0 and self.envelope.timeout_ms:
                object.__setattr__(self.base_policy, "timeout_seconds", self.envelope.timeout_ms / 1000.0)

    @property
    def required_capabilities(self) -> frozenset[str]:
        return self.base_policy.required_capabilities

    @property
    def max_budget(self) -> float | None:
        return self.base_policy.max_budget

    @property
    def max_concurrency(self) -> int:
        return self.base_policy.max_concurrency

    @property
    def timeout_seconds(self) -> float:
        return self.base_policy.timeout_seconds

    @property
    def verification_required(self) -> bool:
        return self.base_policy.verification_required

    @property
    def verification_capability(self) -> str:
        return self.base_policy.verification_capability

    @property
    def allow_escalation(self) -> bool:
        return self.base_policy.allow_escalation

    @property
    def max_escalation_depth(self) -> int:
        return self.base_policy.max_escalation_depth


def create_swarm_dispatcher(
    envelope: SwarmTaskEnvelope,
    fan_out_limiter: FanOutLimiter | None = None,
) -> SwarmDispatcher:
    """Factory for creating a swarm dispatcher with envelope and fan-out config."""
    policy = SwarmDispatchPolicy(envelope=envelope)
    return SwarmDispatcher(policy=policy, fan_out_limiter=fan_out_limiter)


def dispatch_swarm_task(
    envelope: Any,
    snapshot: AvailabilitySnapshot,
    now: datetime | None = None,
) -> Any:
    """
    High-level function to dispatch a swarm task.

    Args:
        envelope: Swarm task envelope with constraints
        snapshot: Availability snapshot with candidates
        now: Optional timestamp for deterministic results

    Returns:
        DispatchResult with selected candidate or None
    """
    from verdict.swarm_contracts import SwarmTaskEnvelope

    if not isinstance(envelope, SwarmTaskEnvelope):
        raise TypeError("envelope must be a SwarmTaskEnvelope")

    # Build dispatch policy from envelope
    policy = SwarmDispatchPolicy(envelope=envelope)

    # Create dispatcher and dispatch
    dispatcher = SwarmDispatcher(policy=policy)
    return dispatcher.dispatch(snapshot, now)


def create_swarm_dispatch_policy(envelope: Any) -> SwarmDispatchPolicy:
    """Factory for creating swarm dispatch policy from envelope."""
    return SwarmDispatchPolicy(envelope=envelope)
