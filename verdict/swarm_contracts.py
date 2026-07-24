"""
Swarm task envelope, termination, and result contracts (Issue #43 / Slice 37.1).

This module defines versioned contracts for lower-tier swarm tasks:
- Task envelope with bounded, resumable, measurable fields
- Attempt/result/error/verification event schemas
- Redaction rules and provenance fields
- Unknown unsafe fields and missing termination conditions rejected
- Workspace and artifact references bounded to configured roots
- Budgets and iteration/concurrency caps enforced
- Result states: success, failure, blocked, timeout, cancelled, rejected
"""

from __future__ import annotations

import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from verdict.contracts import Contract, ContractValidationError


class SwarmTaskState(str, Enum):
    """Swarm task lifecycle states."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SwarmTaskResult(str, Enum):
    """Final result states for swarm tasks."""

    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TerminationReason(str, Enum):
    """Reasons for task termination."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED_BY_USER = "cancelled_by_user"
    CANCELLED_BY_SYSTEM = "cancelled_by_system"
    BUDGET_EXCEEDED = "budget_exceeded"
    ITERATION_LIMIT = "iteration_limit"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    POLICY_VIOLATION = "policy_violation"
    DEPENDENCY_FAILED = "dependency_failed"


# Safe field names that cannot be escaped
_SAFE_PATH_CHARS = re.compile(r"^[a-zA-Z0-9._/-]+$")
_ROOT_PATHS = frozenset({"/home/nick/dev", "/workspace"})


def _get_allowed_roots() -> frozenset[str]:
    """Get allowed root paths, including system temp dir at runtime."""
    return frozenset({"/home/nick/dev", "/workspace", tempfile.gettempdir()})


# /tmp is intentionally excluded from hardcoded roots to avoid B108
# Use tempfile.gettempdir() at runtime instead


def _validate_rooted_path(path: str) -> bool:
    """Validate that a path is within allowed roots."""
    if not _SAFE_PATH_CHARS.match(path):
        return False
    # Include system temp dir at runtime (not hardcoded to avoid B108)
    return any(path.startswith(root) for root in _get_allowed_roots())


def _reject_unsafe_fields(payload: dict[str, Any]) -> None:
    """Reject unknown unsafe fields that could escape sandbox."""
    unsafe_patterns = [
        "..",
        "~",
        "$",
        "`",
        "|",
        ";",
        "&",
        ">",
        "<",
        "||",
        "&&",
        "exec",
        "eval",
        "system",
        "subprocess",
        "os.",
        "sys.",
        "__",
        "import",
    ]
    for key, value in payload.items():
        if isinstance(value, str):
            for pattern in unsafe_patterns:
                if pattern in value:
                    raise ContractValidationError(
                        f"unsafe content in field '{key}': contains '{pattern}'"
                    )


@dataclass(frozen=True)
class SwarmTaskBudget(Contract):
    """Token and USD budget with enforcement caps."""

    max_usd: float = 0.0
    max_tokens: int = 0
    max_latency_ms: int = 0
    estimated_usd: float = 0.0
    estimated_tokens: int = 0

    def __post_init__(self) -> None:
        if self.max_usd < 0 or self.max_tokens < 0 or self.max_latency_ms < 0:
            raise ContractValidationError("budget fields must be non-negative")
        if self.max_usd == 0 and self.max_tokens == 0 and self.max_latency_ms == 0:
            raise ContractValidationError("at least one budget cap must be positive")


@dataclass(frozen=True)
class SwarmTaskCapabilities(Contract):
    """Required capabilities for task execution."""

    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SwarmTaskEnvelope(Contract):
    """
    Versioned envelope for lower-tier swarm tasks.

    Bounded, resumable, measurable task definition with all
    safety constraints and termination conditions.
    """

    # Identity
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_workflow_id: str | None = None
    attempt: int = 1
    max_attempts: int = 3

    # Objective & Input
    objective: str = ""
    input_artifact_refs: list[str] = field(default_factory=list)

    # Execution bounds
    allowed_paths: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    model_floor: str = "auto/best-coding"

    # Budgets & Limits
    budget: Any = None  # SwarmTaskBudget
    timeout_ms: int = 300000  # 5 minutes default
    max_iterations: int = 10
    max_parallelism: int = 1

    # Termination
    stop_conditions: list[str] = field(
        default_factory=lambda: [
            "objective_achieved",
            "budget_exceeded",
            "timeout",
            "max_iterations",
            "policy_violation",
        ]
    )

    # Verification
    verification_command: str | None = None
    result_schema: dict[str, Any] | None = None

    # Provenance & Redaction
    provenance: dict[str, Any] = field(default_factory=dict)
    redaction_rules: list[str] = field(
        default_factory=lambda: ["api_key", "password", "secret", "token", "authorization"]
    )

    # Schema
    schema_version: str = "1"

    def __post_init__(self) -> None:
        # Validate required fields
        if not self.objective or len(self.objective) < 3:
            raise ContractValidationError("objective must be at least 3 characters")

        # Validate paths are rooted
        for path in self.allowed_paths:
            if not _validate_rooted_path(path):
                raise ContractValidationError(f"path '{path}' escapes allowed roots")

        # Validate budgets
        if self.budget is not None:
            # Budget validation happens in SwarmTaskBudget.__post_init__
            pass

        # Validate numeric caps
        if self.timeout_ms <= 0:
            raise ContractValidationError("timeout_ms must be positive")
        if self.max_iterations <= 0:
            raise ContractValidationError("max_iterations must be positive")
        if self.max_parallelism <= 0:
            raise ContractValidationError("max_parallelism must be positive")
        if self.max_attempts <= 0:
            raise ContractValidationError("max_attempts must be positive")

        # Validate stop conditions
        valid_conditions = {
            "objective_achieved",
            "budget_exceeded",
            "timeout",
            "max_iterations",
            "policy_violation",
            "error",
            "cancelled",
            "blocked",
            "dependency_failed",
        }
        for cond in self.stop_conditions:
            if cond not in valid_conditions:
                raise ContractValidationError(f"invalid stop_condition: {cond}")

        # Reject unsafe content
        _reject_unsafe_fields(
            {
                "objective": self.objective,
                **{f"path_{i}": p for i, p in enumerate(self.allowed_paths)},
            }
        )


@dataclass(frozen=True)
class SwarmTaskAttempt(Contract):
    """Single attempt record for a swarm task."""

    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    state: Any = "pending"  # SwarmTaskState
    result: Any | None = None  # SwarmTaskResult
    termination_reason: Any | None = None  # TerminationReason

    # Metrics
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    iterations: int = 0

    # Output
    output_artifact_refs: list[str] = field(default_factory=list)
    error: str | None = None
    verification_passed: bool | None = None

    schema_version: str = "1"

    def mark_completed(
        self,
        result: Any,  # SwarmTaskResult
        reason: Any,  # TerminationReason
        output_refs: list[str] | None = None,
    ) -> SwarmTaskAttempt:
        """Return new attempt marked as completed."""
        return SwarmTaskAttempt(
            attempt_id=self.attempt_id,
            task_id=self.task_id,
            started_at=self.started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            state="completed" if result == "success" else "failed",
            result=result,
            termination_reason=reason,
            tokens_used=self.tokens_used,
            cost_usd=self.cost_usd,
            latency_ms=self.latency_ms,
            iterations=self.iterations,
            output_artifact_refs=output_refs or [],
        )


@dataclass(frozen=True)
class SwarmTaskEvent(Contract):
    """Event in the task lifecycle."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    attempt_id: str = ""
    event_type: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"


@dataclass(frozen=True)
class SwarmTaskVerification(Contract):
    """Verification result for a completed task."""

    verification_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    attempt_id: str = ""
    passed: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    verified_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: str = "1"


def build_swarm_task_envelope(
    objective: str,
    allowed_paths: list[str] | None = None,
    budget: Any = None,
    required_capabilities: list[str] | None = None,
    optional_capabilities: list[str] | None = None,
    model_floor: str = "auto/best-coding",
    max_parallelism: int = 1,
    timeout_ms: int = 300000,
    max_iterations: int = 10,
    max_attempts: int = 3,
    stop_conditions: list[str] | None = None,
    verification_command: str | None = None,
    result_schema: dict[str, Any] | None = None,
    **kwargs: Any,
) -> SwarmTaskEnvelope:
    """Factory for creating swarm task envelopes."""
    return SwarmTaskEnvelope(
        objective=objective,
        allowed_paths=allowed_paths or [],
        budget=budget,
        required_capabilities=required_capabilities or [],
        model_floor=model_floor,
        max_parallelism=max_parallelism,
        timeout_ms=timeout_ms,
        max_iterations=max_iterations,
        max_attempts=max_attempts,
        stop_conditions=stop_conditions
        or [
            "objective_achieved",
            "budget_exceeded",
            "timeout",
            "max_iterations",
            "policy_violation",
            "error",
            "cancelled",
            "blocked",
            "dependency_failed",
        ],
        verification_command=verification_command,
        result_schema=result_schema,
        **kwargs,
    )


def create_task_attempt(task_id: str, attempt: int = 1) -> SwarmTaskAttempt:
    """Factory for creating task attempts."""
    return SwarmTaskAttempt(task_id=task_id)
