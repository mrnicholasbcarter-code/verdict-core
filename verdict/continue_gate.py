"""
Continue Gate for Verdict Core Guidance Control Plane

Evaluates whether to continue, checkpoint, throttle, pause, or stop
after every autonomous step. Integrates with Ruflo Completion Autopilot
for loop detection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContinueState(Enum):
    """Possible continue gate decisions."""
    CONTINUE = "continue"
    CHECKPOINT = "checkpoint"
    THROTTLE = "throttle"
    PAUSE = "pause"
    STOP = "stop"


@dataclass
class LoopDetectionState:
    """State for loop detection across steps."""
    recent_actions: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    step_count: int = 0
    repeated_approaches: dict[str, int] = field(default_factory=dict)
    last_checkpoint_step: int = 0
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    start_time: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """Result of a single execution step."""
    step_number: int
    action: str
    action_signature: str
    status: str  # success, failed, partial
    output: str | None = None
    error: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContinueDecision:
    """Decision from the continue gate."""
    state: ContinueState
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def continue_(cls, reason: str = "Continuing", **metadata) -> ContinueDecision:
        return cls(state=ContinueState.CONTINUE, reason=reason, metadata=metadata)

    @classmethod
    def checkpoint(cls, reason: str = "Checkpoint", **metadata) -> ContinueDecision:
        return cls(state=ContinueState.CHECKPOINT, reason=reason, metadata=metadata)

    @classmethod
    def throttle(cls, reason: str = "Throttling", **metadata) -> ContinueDecision:
        return cls(state=ContinueState.THROTTLE, reason=reason, metadata=metadata)

    @classmethod
    def pause(cls, reason: str = "Paused", **metadata) -> ContinueDecision:
        return cls(state=ContinueState.PAUSE, reason=reason, metadata=metadata)

    @classmethod
    def stop(cls, reason: str = "Stopped", **metadata) -> ContinueDecision:
        return cls(state=ContinueState.STOP, reason=reason, metadata=metadata)


class ContinueGate:
    """
    Continue Gate - evaluates after every autonomous step.

    Supports: Continue, Checkpoint, Throttle, Pause, Stop
    Integrates with Ruflo Completion Autopilot for loop detection.
    """

    def __init__(
        self,
        max_steps_per_task: int = 50,
        max_consecutive_failures: int = 3,
        checkpoint_interval: int = 10,
        throttle_threshold_steps: int = 40,
        max_tokens_per_task: int = 500000,
        max_cost_usd_per_task: float = 5.0,
        max_duration_seconds: int = 3600,
        loop_detection_window: int = 5,
        approach_repeat_threshold: int = 3
    ):
        self.max_steps_per_task = max_steps_per_task
        self.max_consecutive_failures = max_consecutive_failures
        self.checkpoint_interval = checkpoint_interval
        self.throttle_threshold_steps = throttle_threshold_steps
        self.max_tokens_per_task = max_tokens_per_task
        self.max_cost_usd_per_task = max_cost_usd_per_task
        self.max_duration_seconds = max_duration_seconds
        self.loop_detection_window = loop_detection_window
        self.approach_repeat_threshold = approach_repeat_threshold

        self._task_states: dict[str, LoopDetectionState] = {}

    def get_or_create_state(self, task_id: str) -> LoopDetectionState:
        """Get or create loop detection state for a task."""
        if task_id not in self._task_states:
            self._task_states[task_id] = LoopDetectionState()
        return self._task_states[task_id]

    def evaluate(
        self,
        task_id: str,
        step_result: StepResult,
        task_metadata: dict[str, Any] | None = None
    ) -> ContinueDecision:
        """
        Evaluate whether to continue after a step.

        This is the main entry point - call after every autonomous step.
        """
        state = self.get_or_create_state(task_id)
        state.step_count = step_result.step_number
        state.total_tokens_used += step_result.tokens_used
        state.total_cost_usd += step_result.cost_usd

        # 1. Check hard limits first
        hard_limit = self._check_hard_limits(state, step_result)
        if hard_limit:
            return hard_limit

        # 2. Check for loop detection
        loop_decision = self._check_loop_detection(state, step_result)
        if loop_decision:
            return loop_decision

        # 3. Check failure patterns
        failure_decision = self._check_failures(state, step_result)
        if failure_decision:
            return failure_decision

        # 4. Check for checkpoint
        checkpoint_decision = self._check_checkpoint(state, step_result)
        if checkpoint_decision:
            return checkpoint_decision

        # 5. Check for throttle
        throttle_decision = self._check_throttle(state, step_result)
        if throttle_decision:
            return throttle_decision

        # 6. Default: continue
        return ContinueDecision.continue_(
            reason="Step completed successfully",
            step=step_result.step_number,
            tokens_used=state.total_tokens_used,
            cost_usd=state.total_cost_usd
        )

    def _check_hard_limits(self, state: LoopDetectionState, step_result: StepResult) -> ContinueDecision | None:
        """Check hard limits that trigger immediate STOP."""

        # Max steps
        if state.step_count >= self.max_steps_per_task:
            return ContinueDecision.stop(
                reason=f"Maximum steps ({self.max_steps_per_task}) exceeded",
                step=state.step_count,
                limit="max_steps"
            )

        # Max tokens
        if state.total_tokens_used >= self.max_tokens_per_task:
            return ContinueDecision.stop(
                reason=f"Maximum tokens ({self.max_tokens_per_task}) exceeded",
                tokens_used=state.total_tokens_used,
                limit="max_tokens"
            )

        # Max cost
        if state.total_cost_usd >= self.max_cost_usd_per_task:
            return ContinueDecision.stop(
                reason=f"Maximum cost (${self.max_cost_usd_per_task}) exceeded",
                cost_usd=state.total_cost_usd,
                limit="max_cost"
            )

        # Max duration
        elapsed = time.time() - state.start_time
        if elapsed >= self.max_duration_seconds:
            return ContinueDecision.stop(
                reason=f"Maximum duration ({self.max_duration_seconds}s) exceeded",
                elapsed_seconds=elapsed,
                limit="max_duration"
            )

        return None

    def _check_loop_detection(self, state: LoopDetectionState, step_result: StepResult) -> ContinueDecision | None:
        """Detect loops in execution pattern."""
        signature = step_result.action_signature
        if not signature:
            return None

        # Track approach repetitions
        count = state.repeated_approaches.get(signature, 0) + 1
        state.repeated_approaches[signature] = count

        # Check if same approach repeated too many times
        if count >= self.approach_repeat_threshold:
            return ContinueDecision.stop(
                reason=f"Same approach repeated {count} times - possible loop detected",
                action_signature=signature,
                repeat_count=count,
                loop_detected=True
            )

        # Check recent actions for immediate repetition
        state.recent_actions.append(signature)
        if len(state.recent_actions) > self.loop_detection_window:
            state.recent_actions = state.recent_actions[-self.loop_detection_window:]

        # Check if action appears multiple times in recent window
        recent_count = state.recent_actions.count(signature)
        if recent_count >= 3:  # Same action 3+ times in recent window
            return ContinueDecision.stop(
                reason=f"Action repeated {recent_count} times in recent window - loop detected",
                action_signature=signature,
                recent_count=recent_count,
                loop_detected=True
            )

        return None

    def _check_failures(self, state: LoopDetectionState, step_result: StepResult) -> ContinueDecision | None:
        """Check failure patterns."""
        if step_result.status == "failed":
            state.consecutive_failures += 1

            if state.consecutive_failures >= self.max_consecutive_failures:
                return ContinueDecision.stop(
                    reason=f"{state.consecutive_failures} consecutive failures - stopping",
                    consecutive_failures=state.consecutive_failures
                )
            elif state.consecutive_failures >= 2:
                return ContinueDecision.throttle(
                    reason=f"{state.consecutive_failures} consecutive failures - throttling",
                    consecutive_failures=state.consecutive_failures
                )
        else:
            # Reset on success
            state.consecutive_failures = 0

        return None

    def _check_checkpoint(self, state: LoopDetectionState, step_result: StepResult) -> ContinueDecision | None:
        """Check if we should checkpoint."""
        steps_since_checkpoint = state.step_count - state.last_checkpoint_step

        if steps_since_checkpoint >= self.checkpoint_interval:
            state.last_checkpoint_step = state.step_count
            return ContinueDecision.checkpoint(
                reason=f"Checkpoint at step {state.step_count}",
                step=state.step_count,
                checkpoint_interval=self.checkpoint_interval
            )

        return None

    def _check_throttle(self, state: LoopDetectionState, step_result: StepResult) -> ContinueDecision | None:
        """Check if we should throttle."""
        if state.step_count >= self.throttle_threshold_steps:
            return ContinueDecision.throttle(
                reason=f"Approaching step limit ({self.throttle_threshold_steps}/{self.max_steps_per_task}) - throttling",
                step=state.step_count,
                throttle_threshold=self.throttle_threshold_steps
            )

        # Also throttle if token usage is high
        token_ratio = state.total_tokens_used / self.max_tokens_per_task
        if token_ratio >= 0.8:
            return ContinueDecision.throttle(
                reason=f"Token usage at {token_ratio:.0%} of limit - throttling",
                tokens_used=state.total_tokens_used,
                token_limit=self.max_tokens_per_task,
                ratio=token_ratio
            )

        # Throttle if cost is high
        cost_ratio = state.total_cost_usd / self.max_cost_usd_per_task
        if cost_ratio >= 0.8:
            return ContinueDecision.throttle(
                reason=f"Cost at {cost_ratio:.0%} of limit - throttling",
                cost_usd=state.total_cost_usd,
                cost_limit=self.max_cost_usd_per_task,
                ratio=cost_ratio
            )

        return None

    def force_pause(self, task_id: str, reason: str = "Manually paused") -> ContinueDecision:
        """Force a pause for a task."""
        return ContinueDecision.pause(reason=reason, task_id=task_id)

    def force_stop(self, task_id: str, reason: str = "Manually stopped") -> ContinueDecision:
        """Force a stop for a task."""
        return ContinueDecision.stop(reason=reason, task_id=task_id)

    def reset_task(self, task_id: str):
        """Reset state for a task."""
        if task_id in self._task_states:
            del self._task_states[task_id]

    def get_state_summary(self, task_id: str) -> dict[str, Any]:
        """Get summary of task state for monitoring."""
        state = self._task_states.get(task_id)
        if not state:
            return {"task_id": task_id, "state": "not_started"}

        return {
            "task_id": task_id,
            "step_count": state.step_count,
            "consecutive_failures": state.consecutive_failures,
            "total_tokens_used": state.total_tokens_used,
            "total_cost_usd": state.total_cost_usd,
            "elapsed_seconds": time.time() - state.start_time,
            "recent_actions": state.recent_actions[-5:],
            "repeated_approaches": {
                k: v for k, v in state.repeated_approaches.items() if v > 1
            },
            "last_checkpoint_step": state.last_checkpoint_step
        }


class AutopilotIntegration:
    """
    Integration with Ruflo Completion Autopilot.

    This class provides hooks for the autopilot to query continue gate state
    and coordinate with Ruflo's autonomous completion logic.
    """

    def __init__(self, continue_gate: ContinueGate):
        self.continue_gate = continue_gate

    async def should_continue_autopilot(self, task_id: str) -> tuple[bool, str]:
        """
        Check if autopilot should continue for a task.

        Returns (should_continue, reason).
        """
        state = self.continue_gate.get_or_create_state(task_id)

        # Check if task is in a terminal state
        if state.step_count >= self.continue_gate.max_steps_per_task:
            return False, "Max steps reached"

        if state.consecutive_failures >= self.continue_gate.max_consecutive_failures:
            return False, "Too many failures"

        if state.total_tokens_used >= self.continue_gate.max_tokens_per_task:
            return False, "Token budget exhausted"

        if state.total_cost_usd >= self.continue_gate.max_cost_usd_per_task:
            return False, "Cost budget exhausted"

        elapsed = time.time() - state.start_time
        if elapsed >= self.continue_gate.max_duration_seconds:
            return False, "Time budget exhausted"

        return True, "Autopilot may continue"

    async def get_autopilot_status(self, task_id: str) -> dict[str, Any]:
        """Get detailed status for autopilot dashboard."""
        state = self.continue_gate.get_or_create_state(task_id)
        should_continue, reason = await self.should_continue_autopilot(task_id)

        return {
            "task_id": task_id,
            "should_continue": should_continue,
            "reason": reason,
            "state_summary": self.continue_gate.get_state_summary(task_id),
            "limits": {
                "max_steps": self.continue_gate.max_steps_per_task,
                "max_tokens": self.continue_gate.max_tokens_per_task,
                "max_cost_usd": self.continue_gate.max_cost_usd_per_task,
                "max_duration_seconds": self.continue_gate.max_duration_seconds
            },
            "current": {
                "steps": state.step_count,
                "tokens": state.total_tokens_used,
                "cost_usd": state.total_cost_usd,
                "elapsed_seconds": time.time() - state.start_time
            }
        }

    async def handle_autopilot_checkpoint(self, task_id: str) -> bool:
        """
        Handle autopilot checkpoint request.

        Returns True if checkpoint was created, False if task should stop.
        """
        # The autopilot requests a checkpoint - we evaluate if we should
        # create one or if the task has hit limits
        should_continue, reason = await self.should_continue_autopilot(task_id)

        if not should_continue:
            return False

        # Record checkpoint
        state = self.continue_gate.get_or_create_state(task_id)
        state.last_checkpoint_step = state.step_count

        return True


__all__ = [
    "AutopilotIntegration",
    "ContinueDecision",
    "ContinueGate",
    "ContinueState",
    "LoopDetectionState",
    "StepResult",
]
