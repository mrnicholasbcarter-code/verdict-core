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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from verdict.contracts import AvailabilitySnapshot, RuntimeCandidate
from verdict.dispatcher import DispatchPolicy, DispatchResult
from verdict.dispatcher import SwarmDispatcher as BaseSwarmDispatcher
from verdict.execution_path import ExecutionPathDecision, ExecutionPathError
from verdict.serve_path import match_candidate_to_selected_route
from verdict.session_economics import ConcreteRoute
from verdict.swarm_contracts import SwarmTaskEnvelope


class _BoundView:
    """Attribute view over a mapping so effective_bounds can read dict limits."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __getattr__(self, name: str) -> Any:
        try:
            return self._payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


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
        return (
            self._active_count >= self.max_concurrent
            or len(self._queue) >= self.max_queue_depth * 0.8
        )


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
    Swarm-aware execution binder with bounded fan-out and backpressure.

    BOD-104 owns strategy/route selection. BOD-67 owns dispatch authorization.
    This module only validates eligibility of an already-authorized
    ``selected_route`` against an availability snapshot — it never invents
    or re-ranks models (BOD-127).
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
            max_queue_depth=policy.max_queue_depth,
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
        self, candidates: list[RuntimeCandidate], envelope: Any
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
        *,
        selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision | None = None,
        authorized_runtime_id: str | None = None,
    ) -> Any:
        """Bind ``selected_route`` after envelope/fan-out gates; never invent."""

        envelope = getattr(self.policy, "envelope", None)

        if selected_route is None and (
            not authorized_runtime_id or not str(authorized_runtime_id).strip()
        ):
            return DispatchResult(
                selected=None,
                explanations=(),
                eligible=(),
                dry_run=True,
                reason="missing_authorized_selected_route",
                estimated_cost=0.0,
                escalation_depth=0,
            )

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

        try:
            result = self._base_dispatcher.dispatch(
                snapshot,
                now=now,
                selected_route=selected_route,
                authorized_runtime_id=authorized_runtime_id,
            )

            if envelope is None:
                return result

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

            # Re-bind authorized route within envelope filter — never re-rank.
            if selected_route is not None:
                selected = match_candidate_to_selected_route(filtered, selected_route)
            else:
                selected = next(
                    (c for c in filtered if c.runtime_id == authorized_runtime_id), None
                )
                if selected is None:
                    raise ExecutionPathError(
                        f"no candidate matches authorized_runtime_id={authorized_runtime_id!r} "
                        "after envelope filtering; swarm must not invent an alternate"
                    )

            cost = self._get_candidate_cost(selected) or 0.0
            return DispatchResult(
                selected=selected,
                explanations=result.explanations,
                eligible=tuple(filtered),
                dry_run=True,
                reason="selected_route",
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
        *,
        selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision | None = None,
        authorized_runtime_id: str | None = None,
    ) -> Any:
        """Async dispatch with fan-out queue; still requires selected_route."""

        if not self.fan_out.try_acquire():
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
            return self.dispatch(
                snapshot,
                now,
                task_id,
                selected_route=selected_route,
                authorized_runtime_id=authorized_runtime_id,
            )
        finally:
            self.fan_out.release()

    def iterate_lower_tier(
        self,
        snapshot: AvailabilitySnapshot,
        task_id: str,
        now: datetime | None = None,
        max_escalation_depth: int | None = None,
        *,
        selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision | None = None,
        authorized_runtime_id: str | None = None,
    ) -> Any:
        """Retry the same authorized route under escalation depth bounds.

        Escalation may deepen attempts/timeouts but must not invent a different
        model/provider — that remains BOD-104 + BOD-55 authority.
        """

        policy = self.policy.base_policy

        if max_escalation_depth is None:
            max_escalation_depth = policy.max_escalation_depth

        state = self._iteration_state.get(task_id)
        if state is None:
            state = IterationState()
            self._iteration_state[task_id] = state

        state.attempt += 1
        start = time.time()

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

        result = self.dispatch(
            snapshot,
            now,
            task_id,
            selected_route=selected_route,
            authorized_runtime_id=authorized_runtime_id,
        )

        state.last_result = result
        state.last_elapsed = time.time() - start

        # Depth tracking only — never swap to a different unauthorized route.
        if result.selected is None and state.escalation_depth < max_escalation_depth:
            state.escalation_depth += 1
            return self.iterate_lower_tier(
                snapshot,
                task_id,
                now,
                max_escalation_depth,
                selected_route=selected_route,
                authorized_runtime_id=authorized_runtime_id,
            )

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
    narrowing_limits: tuple[Any, ...] = ()
    _max_queue_depth: int = field(default=100, init=False, repr=False)

    # Budget enforcement
    enforce_budget: bool = True
    enforce_capabilities: bool = True
    enforce_stop_conditions: bool = True

    def __post_init__(self) -> None:
        if self.envelope is not None:
            bounds = self.effective_bounds(self.envelope, *self.narrowing_limits)
            max_concurrency = bounds["max_concurrency"]
            timeout_ms = bounds["timeout_ms"]
            if max_concurrency is None or timeout_ms is None:
                raise ValueError("effective dispatcher bounds must be finite")
            object.__setattr__(self.base_policy, "max_concurrency", int(max_concurrency))
            object.__setattr__(self.base_policy, "timeout_seconds", timeout_ms / 1000.0)
            if bounds["max_usd"] is not None:
                object.__setattr__(self.base_policy, "max_budget", bounds["max_usd"])
            object.__setattr__(self, "_max_queue_depth", int(bounds["max_queue_depth"] or 1))
            required = set(self.base_policy.required_capabilities)
            for source in (self.envelope, *self.narrowing_limits):
                if source is None:
                    continue
                caps = getattr(source, "required_capabilities", None)
                if caps:
                    required |= set(caps)
            if required:
                object.__setattr__(self.base_policy, "required_capabilities", frozenset(required))

    @classmethod
    def from_swarm_bounds(
        cls,
        envelope: Any,
        *,
        swarm: Any | None = None,
        role: Any | None = None,
        slice_limit: Any | None = None,
        base_policy: DispatchPolicy | None = None,
    ) -> SwarmDispatchPolicy:
        """Build policy from a validated envelope plus swarm/role/slice bounds."""
        narrowing: list[Any] = []
        for source in (swarm, role, slice_limit):
            if source is None:
                continue
            narrowing.append(source if not isinstance(source, dict) else _BoundView(source))
        kwargs: dict[str, Any] = {"envelope": envelope, "narrowing_limits": tuple(narrowing)}
        if base_policy is not None:
            kwargs["base_policy"] = base_policy
        return cls(**kwargs)

    def effective_bounds(
        self, envelope: Any, *narrowing_limits: Any
    ) -> dict[str, float | int | None]:
        """Return strict minimum bounds across envelope, optional owners, and dispatcher policy."""
        sources = (envelope, *narrowing_limits)
        concurrency = [self.base_policy.max_concurrency]
        # The envelope is authoritative when the base policy retains its
        # compatibility default of one and the envelope explicitly widens it.
        if self.base_policy.max_concurrency == 1 and getattr(envelope, "max_parallelism", None):
            concurrency = []
        timeout_ms = [self.base_policy.timeout_seconds * 1000.0]
        queue_depths = [100]
        budgets: list[float] = []
        for source in sources:
            if source is None:
                continue
            concurrency_value = getattr(
                source, "max_concurrency", getattr(source, "max_parallelism", None)
            )
            if concurrency_value is not None:
                concurrency.append(int(concurrency_value))
            timeout_value = getattr(source, "timeout_ms", None)
            if timeout_value is None and hasattr(source, "timeout_seconds"):
                timeout_value = float(source.timeout_seconds) * 1000.0
            if timeout_value is not None:
                timeout_ms.append(float(timeout_value))
            queue_depth = getattr(source, "max_queue_depth", None)
            if queue_depth is not None:
                queue_depths.append(int(queue_depth))
            budget = getattr(source, "budget", None)
            amount = getattr(budget, "max_usd", None)
            if amount is not None and amount > 0:
                budgets.append(float(amount))
            amount = getattr(source, "max_budget", None)
            if amount is not None and amount > 0:
                budgets.append(float(amount))
        return {
            "max_concurrency": min(concurrency),
            "timeout_ms": min(timeout_ms),
            "max_usd": min(budgets) if budgets else None,
            "max_queue_depth": min(queue_depths),
        }

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
    def max_queue_depth(self) -> int:
        return self._max_queue_depth

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
    envelope: SwarmTaskEnvelope, fan_out_limiter: FanOutLimiter | None = None
) -> SwarmDispatcher:
    """Factory for creating a swarm dispatcher with envelope and fan-out config."""
    policy = SwarmDispatchPolicy(envelope=envelope)
    return SwarmDispatcher(policy=policy, fan_out_limiter=fan_out_limiter)


def dispatch_swarm_task(
    envelope: Any,
    snapshot: AvailabilitySnapshot,
    now: datetime | None = None,
    *,
    selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision | None = None,
    authorized_runtime_id: str | None = None,
) -> Any:
    """Dispatch a swarm task using an already-authorized selected_route."""
    from verdict.swarm_contracts import SwarmTaskEnvelope

    if not isinstance(envelope, SwarmTaskEnvelope):
        raise TypeError("envelope must be a SwarmTaskEnvelope")

    policy = SwarmDispatchPolicy(envelope=envelope)
    dispatcher = SwarmDispatcher(policy=policy)
    return dispatcher.dispatch(
        snapshot, now, selected_route=selected_route, authorized_runtime_id=authorized_runtime_id
    )


def create_swarm_dispatch_policy(envelope: Any) -> SwarmDispatchPolicy:
    """Factory for creating swarm dispatch policy from envelope."""
    return SwarmDispatchPolicy(envelope=envelope)


def dispatch_governed_swarm(
    spec_payload: Any,
    *,
    envelope: SwarmTaskEnvelope,
    snapshot: AvailabilitySnapshot,
    dispatcher: Any | None = None,
    adapter: Any | None = None,
    role: Any | None = None,
    slice_limit: Any | None = None,
    now: datetime | None = None,
    selected_route: ConcreteRoute | Mapping[str, Any] | ExecutionPathDecision | None = None,
    authorized_runtime_id: str | None = None,
) -> Any:
    """
    Validate a SwarmSpec before any dispatcher or adapter interaction.

    Invalid specs fail closed and never reach dispatch/submit call sites.
    Dispatch still requires an authorized selected_route (BOD-127).
    """
    from verdict.swarm_governance import SwarmSpec

    if isinstance(spec_payload, SwarmSpec):
        validated = spec_payload
    else:
        validated = SwarmSpec.from_dict(dict(spec_payload))

    policy = SwarmDispatchPolicy.from_swarm_bounds(
        envelope, swarm=validated, role=role, slice_limit=slice_limit
    )
    active_dispatcher = dispatcher if dispatcher is not None else SwarmDispatcher(policy=policy)
    result = active_dispatcher.dispatch(
        snapshot, now, selected_route=selected_route, authorized_runtime_id=authorized_runtime_id
    )
    if adapter is not None:
        adapter.submit(validated)
    return result
