"""
Ruflo Adapter Boundary v1 - Protocol/Types for Ruflo Orchestration Interface

Implements #38: Versioned protocol boundary between Verdict and Ruflo orchestration.
Provides:
- Submit/Status/Pause/Resume/Cancel/Approval/Result typed operations
- Capability manifest for declarative capability requirements
- Fake adapter for deterministic testing (no network credentials required)
- Real transport implementations for production use
- Trust boundaries: Verdict never bypasses Ruflo's authority on protected work

This module does NOT implement Ruflo itself - it defines the contract Verdict uses
to communicate with any Ruflo-compatible orchestration backend.
"""

from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from verdict.contracts import TaskSpec, WorkflowPlan
from verdict.ruflo_transport import RufloTransport
from verdict.swarm_runtime import (
    SUPPORTED_RUNTIME_PROTOCOL_VERSIONS,
    RuntimeFailure,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeState,
)

RUFLO_ADAPTER_PROTOCOL_VERSION = "rufl-adapter/v1"
SUPPORTED_PROTOCOL_VERSIONS = ["rufl-adapter/v1"]


def negotiate_protocol_version(requested: str) -> str:
    """Negotiate protocol version, falling back to latest supported."""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return RUFLO_ADAPTER_PROTOCOL_VERSION


class RufloAdapterError(Exception):
    """Base exception for Ruflo adapter errors."""

    category: str = "rufl_adapter_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class RufloUnavailableError(RufloAdapterError):
    """Ruflo backend unavailable or unreachable."""

    category = "rufl_unavailable"


class RufloProtocolError(RufloAdapterError):
    """Protocol violation - invalid request/response format."""

    category = "rufl_protocol_error"


class RufloCapacityError(RufloAdapterError):
    """Capacity constraints exceeded (budget, concurrency, quotas)."""

    category = "rufl_capacity_exceeded"


class RufloApprovalError(RufloAdapterError):
    """Approval workflow failures."""

    category = "rufl_approval_error"


class RufloCancellationError(RufloAdapterError):
    """Task cancellation failures."""

    category = "rufl_cancellation_error"


class RufloVerificationError(RufloAdapterError):
    """Verification gate failures."""

    category = "rufl_verification_error"


class RufloTimeoutError(RufloAdapterError):
    """Operation timeout."""

    category = "rufl_timeout"


class RufloValidationError(RufloAdapterError):
    """Input validation failures."""

    category = "rufl_validation_error"


@dataclass(frozen=True)
class RufloAdapterConfig:
    """Configuration for Ruflo adapter boundary."""

    # Contract version
    protocol_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION

    # Timeouts (milliseconds)
    submit_timeout_ms: int = 5000
    status_timeout_ms: int = 2000
    control_timeout_ms: int = 3000  # pause/resume/cancel/approve
    result_timeout_ms: int = 10000

    # Retry policy
    max_submit_retries: int = 2
    retry_backoff_base_ms: int = 500
    retry_backoff_max_ms: int = 5000

    # Capability manifest
    required_capabilities: list[str] = field(
        default_factory=lambda: [
            "task_submission",
            "status_query",
            "pause_resume",
            "cancellation",
            "approval_workflow",
            "result_retrieval",
            "verification_gates",
        ]
    )
    optional_capabilities: list[str] = field(
        default_factory=lambda: ["replan", "partial_results", "streaming_updates", "cost_tracking"]
    )

    # Trust boundaries
    trust_protected_work: bool = True  # Verdict never authorizes protected work without Ruflo
    require_verification_for_protected: bool = True
    max_concurrent_tasks: int = 10
    default_budget_usd: float = 50.0

    # Fake adapter mode (for testing)
    fake_mode: bool = False
    fake_latency_ms: int = 10
    fake_failure_rate: float = 0.0


class TaskStatus(enum.Enum):
    """Task lifecycle states in Ruflo orchestration."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class TaskAction(enum.Enum):
    """Control actions that can be sent to Ruflo."""

    SUBMIT = "submit"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    APPROVE = "approve"
    REJECT = "reject"
    REPLAN = "replan"


@dataclass(frozen=True)
class CapabilityManifest:
    """Declarative capability requirements for a task/workflow."""

    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    minimum_versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate no overlap between required/forbidden
        overlap = set(self.required) & set(self.forbidden)
        if overlap:
            raise ValueError(f"Capabilities cannot be both required and forbidden: {overlap}")

    def requires(self, capability: str) -> bool:
        return capability in self.required

    def permits(self, capability: str) -> bool:
        return capability not in self.forbidden

    def satisfies(self, available: list[str]) -> bool:
        """Check if available capabilities satisfy requirements."""
        available_set = set(available)
        return all(req in available_set for req in self.required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "optional": self.optional,
            "forbidden": self.forbidden,
            "minimum_versions": self.minimum_versions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityManifest:
        return cls(
            required=data.get("required", []),
            optional=data.get("optional", []),
            forbidden=data.get("forbidden", []),
            minimum_versions=data.get("minimum_versions", {}),
        )


@dataclass(frozen=True)
class RufloSubmitRequest:
    """Request to submit a task/workflow to Ruflo."""

    task_spec: dict[str, Any]  # TaskSpec.to_dict()
    workflow_plan: dict[str, Any] | None = None  # WorkflowPlan.to_dict()
    capability_manifest: CapabilityManifest | None = None
    budget_usd: float | None = None
    priority: int = 0  # Higher = more urgent
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_spec": self.task_spec,
            "workflow_plan": self.workflow_plan,
            "capability_manifest": self.capability_manifest.to_dict()
            if self.capability_manifest
            else None,
            "budget_usd": self.budget_usd,
            "priority": self.priority,
            "idempotency_key": self.idempotency_key,
            "metadata": self.metadata,
            "contract_version": self.contract_version,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RufloSubmitRequest:
        manifest = data.get("capability_manifest")
        if manifest:
            manifest = CapabilityManifest.from_dict(manifest)
        return cls(
            task_spec=data["task_spec"],
            workflow_plan=data.get("workflow_plan"),
            capability_manifest=manifest,
            budget_usd=data.get("budget_usd"),
            priority=data.get("priority", 0),
            idempotency_key=data.get("idempotency_key", str(uuid.uuid4())),
            metadata=data.get("metadata", {}),
            contract_version=data.get("contract_version", RUFLO_ADAPTER_PROTOCOL_VERSION),
            submitted_at=data.get("submitted_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass(frozen=True)
class RufloSubmitResponse:
    """Response from task submission."""

    task_id: str
    workflow_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    accepted: bool = True
    reason: str = ""
    estimated_start_at: str | None = None
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "accepted": self.accepted,
            "reason": self.reason,
            "estimated_start_at": self.estimated_start_at,
            "contract_version": self.contract_version,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RufloSubmitResponse:
        return cls(
            task_id=data["task_id"],
            workflow_id=data.get("workflow_id"),
            status=TaskStatus(data.get("status", "pending")),
            accepted=data.get("accepted", True),
            reason=data.get("reason", ""),
            estimated_start_at=data.get("estimated_start_at"),
            contract_version=data.get("contract_version", RUFLO_ADAPTER_PROTOCOL_VERSION),
            submitted_at=data.get("submitted_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass(frozen=True)
class RufloStatusRequest:
    """Request for task/workflow status."""

    task_id: str
    workflow_id: str | None = None
    include_history: bool = False
    include_verification: bool = False
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class RufloStatusResponse:
    """Response from status query."""

    task_id: str
    workflow_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    progress_pct: float = 0.0
    current_step: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    steps_pending: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None
    estimated_completion_at: str | None = None
    cost_usd: float = 0.0
    tokens_used: int = 0
    verification_results: list[dict[str, Any]] = field(default_factory=list)
    last_update_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "progress_pct": self.progress_pct,
            "current_step": self.current_step,
            "steps_completed": self.steps_completed,
            "steps_pending": self.steps_pending,
            "steps_failed": self.steps_failed,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "estimated_completion_at": self.estimated_completion_at,
            "cost_usd": self.cost_usd,
            "tokens_used": self.tokens_used,
            "verification_results": self.verification_results,
            "last_update_at": self.last_update_at,
            "error": self.error,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RufloStatusResponse:
        return cls(
            task_id=data["task_id"],
            workflow_id=data.get("workflow_id"),
            status=TaskStatus(data.get("status", "pending")),
            progress_pct=data.get("progress_pct", 0.0),
            current_step=data.get("current_step"),
            steps_completed=data.get("steps_completed", []),
            steps_pending=data.get("steps_pending", []),
            steps_failed=data.get("steps_failed", []),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            estimated_completion_at=data.get("estimated_completion_at"),
            cost_usd=data.get("cost_usd", 0.0),
            tokens_used=data.get("tokens_used", 0),
            verification_results=data.get("verification_results", []),
            last_update_at=data.get("last_update_at", datetime.now(timezone.utc).isoformat()),
            error=data.get("error"),
            contract_version=data.get("contract_version", RUFLO_ADAPTER_PROTOCOL_VERSION),
        )


@dataclass(frozen=True)
class RufloControlRequest:
    """Control request (pause/resume/cancel/approve/reject/replan)."""

    task_id: str
    action: TaskAction
    workflow_id: str | None = None
    reason: str = ""
    approver: str | None = None
    replan_spec: dict[str, Any] | None = None  # For REPLAN action
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class RufloControlResponse:
    """Response from control action."""

    task_id: str
    action: TaskAction
    success: bool
    previous_status: TaskStatus
    new_status: TaskStatus
    reason: str = ""
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action": self.action.value,
            "success": self.success,
            "previous_status": self.previous_status.value,
            "new_status": self.new_status.value,
            "reason": self.reason,
            "contract_version": self.contract_version,
            "executed_at": self.executed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RufloControlResponse:
        return cls(
            task_id=data["task_id"],
            action=TaskAction(data["action"]),
            success=data["success"],
            previous_status=TaskStatus(data["previous_status"]),
            new_status=TaskStatus(data["new_status"]),
            reason=data.get("reason", ""),
            contract_version=data.get("contract_version", RUFLO_ADAPTER_PROTOCOL_VERSION),
            executed_at=data.get("executed_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass(frozen=True)
class RufloResult:
    """Final result of a completed task/workflow."""

    task_id: str
    workflow_id: str | None = None
    status: TaskStatus = TaskStatus.COMPLETED
    outcome: str = "success"  # success, failure, partial, denied, cancelled, timeout
    output_artifacts: list[str] = field(default_factory=list)
    output_data: dict[str, Any] = field(default_factory=dict)
    verification_passed: bool = False
    verification_results: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens_used: int = 0
    latency_ms: int = 0
    started_at: str | None = None
    completed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None
    replan_count: int = 0
    contract_version: str = RUFLO_ADAPTER_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "outcome": self.outcome,
            "output_artifacts": self.output_artifacts,
            "output_data": self.output_data,
            "verification_passed": self.verification_passed,
            "verification_results": self.verification_results,
            "cost_usd": self.cost_usd,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "replan_count": self.replan_count,
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RufloResult:
        return cls(
            task_id=data["task_id"],
            workflow_id=data.get("workflow_id"),
            status=TaskStatus(data.get("status", "completed")),
            outcome=data.get("outcome", "success"),
            output_artifacts=data.get("output_artifacts", []),
            output_data=data.get("output_data", {}),
            verification_passed=data.get("verification_passed", False),
            verification_results=data.get("verification_results", []),
            cost_usd=data.get("cost_usd", 0.0),
            tokens_used=data.get("tokens_used", 0),
            latency_ms=data.get("latency_ms", 0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at", datetime.now(timezone.utc).isoformat()),
            error=data.get("error"),
            replan_count=data.get("replan_count", 0),
            contract_version=data.get("contract_version", RUFLO_ADAPTER_PROTOCOL_VERSION),
        )


class RufloAdapter:
    """
    Ruflo Adapter Boundary - Protocol interface for Ruflo orchestration.

    This is the contract boundary between Verdict and Ruflo. All communication
    goes through typed request/response envelopes with strict validation.

    Trust Boundaries:
    - Verdict NEVER authorizes protected work without Ruflo confirmation
    - Ruflo is the SOLE authority on task lifecycle state
    - Capability manifest is enforced on both sides
    - All operations are idempotent via idempotency keys
    """

    def __init__(
        self,
        config: RufloAdapterConfig | None = None,
        transport: RufloTransport | Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config or RufloAdapterConfig()
        self._transport = transport
        self._capability_manifest = CapabilityManifest(
            required=self.config.required_capabilities, optional=self.config.optional_capabilities
        )
        self._fake_state: dict[str, Any] = {}  # For fake adapter mode

    @property
    def capability_manifest(self) -> CapabilityManifest:
        return self._capability_manifest

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one request through the configured transport.

        Accepts either a :class:`RufloTransport` (method-per-operation) or a bare
        callable taking a ``{"method": ..., "params": ...}`` envelope, so both the
        real transports and the test doubles share one call path.
        """
        transport = self._transport
        if transport is None:
            raise RufloUnavailableError("no transport configured")
        if isinstance(transport, RufloTransport):
            operations: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
                "submit": transport.submit,
                "status": transport.status,
                "control": transport.control,
                "result": transport.result,
            }
            if method not in operations:
                raise RufloUnavailableError(f"unsupported transport method: {method!r}")
            return operations[method](params)
        return transport({"method": method, "params": params})

    def submit(
        self,
        task_spec: TaskSpec,
        workflow_plan: WorkflowPlan | None = None,
        capability_manifest: CapabilityManifest | None = None,
        budget_usd: float | None = None,
        priority: int = 0,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RufloSubmitResponse:
        """Submit a task/workflow to Ruflo for execution."""

        # Build request
        request = RufloSubmitRequest(
            task_spec=task_spec.to_dict(),
            workflow_plan=workflow_plan.to_dict() if workflow_plan else None,
            capability_manifest=capability_manifest,
            budget_usd=budget_usd or self.config.default_budget_usd,
            priority=priority,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            metadata=metadata or {},
        )

        # Validate capability manifest
        if capability_manifest and not capability_manifest.satisfies(
            self._capability_manifest.required
        ):
            missing = set(capability_manifest.required) - set(self._capability_manifest.required)
            raise RufloValidationError(
                f"Capability manifest requires unavailable capabilities: {missing}",
                details={"missing": list(missing), "available": self._capability_manifest.required},
            )

        # Execute via transport or fake
        if self.config.fake_mode or self._transport is None:
            return self._fake_submit(request)

        try:
            response_data = self._dispatch("submit", request.to_dict())
            return RufloSubmitResponse.from_dict(response_data)
        except Exception as e:
            raise RufloUnavailableError(f"Submit failed: {e}") from e

    def status(
        self,
        task_id: str,
        workflow_id: str | None = None,
        include_history: bool = False,
        include_verification: bool = False,
    ) -> RufloStatusResponse:
        """Query task/workflow status from Ruflo."""

        request = RufloStatusRequest(
            task_id=task_id,
            workflow_id=workflow_id,
            include_history=include_history,
            include_verification=include_verification,
        )

        if self.config.fake_mode or self._transport is None:
            return self._fake_status(request)

        try:
            response_data = self._dispatch("status", request.__dict__)
            return RufloStatusResponse.from_dict(response_data)
        except Exception as e:
            raise RufloUnavailableError(f"Status query failed: {e}") from e

    def pause(
        self, task_id: str, workflow_id: str | None = None, reason: str = ""
    ) -> RufloControlResponse:
        """Pause a running task."""
        return self._control(TaskAction.PAUSE, task_id, workflow_id, reason)

    def resume(
        self, task_id: str, workflow_id: str | None = None, reason: str = ""
    ) -> RufloControlResponse:
        """Resume a paused task."""
        return self._control(TaskAction.RESUME, task_id, workflow_id, reason)

    def cancel(
        self, task_id: str, workflow_id: str | None = None, reason: str = ""
    ) -> RufloControlResponse:
        """Cancel a task."""
        return self._control(TaskAction.CANCEL, task_id, workflow_id, reason)

    def approve(
        self, task_id: str, workflow_id: str | None = None, approver: str = "", reason: str = ""
    ) -> RufloControlResponse:
        """Approve a task waiting for approval (protected work)."""
        if not approver:
            raise RufloApprovalError("Approver identity required for approval")
        return self._control(TaskAction.APPROVE, task_id, workflow_id, reason, approver)

    def reject(
        self, task_id: str, workflow_id: str | None = None, approver: str = "", reason: str = ""
    ) -> RufloControlResponse:
        """Reject a task waiting for approval."""
        if not approver:
            raise RufloApprovalError("Approver identity required for rejection")
        return self._control(TaskAction.REJECT, task_id, workflow_id, reason, approver)

    def replan(
        self,
        task_id: str,
        replan_spec: dict[str, Any],
        workflow_id: str | None = None,
        reason: str = "",
    ) -> RufloControlResponse:
        """Request a replan with new specification."""
        return self._control(
            TaskAction.REPLAN, task_id, workflow_id, reason, replan_spec=replan_spec
        )

    def result(self, task_id: str, workflow_id: str | None = None) -> RufloResult:
        """Get final result of a completed task."""

        if self.config.fake_mode or self._transport is None:
            return self._fake_result(task_id, workflow_id)

        try:
            response_data = self._dispatch(
                "result", {"task_id": task_id, "workflow_id": workflow_id}
            )
            return RufloResult.from_dict(response_data)
        except Exception as e:
            raise RufloUnavailableError(f"Result retrieval failed: {e}") from e

    def _governed_response(
        self,
        request: RuntimeRequest,
        task_id: str,
        state: RuntimeState,
        **kwargs: Any,
    ) -> RuntimeResponse:
        metadata = dict(kwargs.pop("metadata", {}) or {})
        metadata.setdefault("observed_bounds", dict(request.approved_bounds))
        metadata.setdefault("evidence_scope", request.metadata.get("evidence_scope"))
        metadata.setdefault("verification_status", "pending")
        kwargs.setdefault("protocol_version", request.protocol_version)
        kwargs.setdefault("swarm_id", request.swarm_id)
        kwargs.setdefault("slice_id", request.slice_id)
        kwargs.setdefault("envelope_digest", request.envelope_digest)
        kwargs.setdefault("operation_id", request.request_id)
        kwargs.setdefault(
            "evidence_ref", f"{request.metadata.get('evidence_scope')}:{task_id}"
        )
        return RuntimeResponse(
            request.request_id,
            task_id,
            state,
            metadata=metadata,
            **kwargs,
        )

    def submit_runtime(self, request: RuntimeRequest) -> RuntimeResponse:
        """Submit a governed Core request without allowing Ruflo to change policy."""
        if request.protocol_version not in SUPPORTED_RUNTIME_PROTOCOL_VERSIONS:
            return RuntimeResponse(
                request.request_id,
                request.task_id,
                RuntimeState.REJECTED,
                protocol_version=request.protocol_version,
                swarm_id=request.swarm_id,
                slice_id=request.slice_id,
                envelope_digest=request.envelope_digest,
                failure=RuntimeFailure(
                    "unsupported_version",
                    "unsupported runtime protocol",
                    category="unsupported_version",
                ),
            )
        try:
            task = TaskSpec(objective=request.objective, task_type="swarm")
            submitted = self.submit(task, idempotency_key=request.request_id,
                                     metadata={"swarm_id": request.swarm_id, "slice_id": request.slice_id,
                                               "envelope_digest": request.envelope_digest,
                                               "approved_bounds": dict(request.approved_bounds)})
            state = RuntimeState.QUEUED if submitted.accepted else RuntimeState.REJECTED
            failure = None if submitted.accepted else RuntimeFailure("submission_rejected", submitted.reason or "submission rejected", category="validation")
            return self._governed_response(
                request,
                submitted.task_id,
                state,
                failure=failure,
            )
        except RufloAdapterError as exc:
            return self._governed_response(
                request,
                request.task_id,
                RuntimeState.FAILED,
                failure=RuntimeFailure(exc.category, str(exc), category="unavailable", retryable=True),
            )

    def runtime_status(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        try:
            status = self.status(task_id, include_verification=True)
            state = RuntimeState.TIMEOUT if status.status == TaskStatus.TIMED_OUT else RuntimeState(status.status.value)
            failure = RuntimeFailure("runtime_failure", status.error, category="unavailable") if status.error else None
            return self._governed_response(
                request,
                task_id,
                state,
                failure=failure,
                metadata={"verification_results": status.verification_results},
            )
        except (ValueError, RufloAdapterError) as exc:
            return self._governed_response(
                request,
                task_id,
                RuntimeState.FAILED,
                failure=RuntimeFailure("malformed_response", str(exc), category="malformed_message"),
            )

    def runtime_control(self, request: RuntimeRequest, task_id: str, action: str) -> RuntimeResponse:
        if action not in request.allowed_controls:
            return self._governed_response(
                request,
                task_id,
                RuntimeState.FAILED,
                failure=RuntimeFailure(
                    "unauthorized_control",
                    "control is not allowed",
                    category="unauthorized_control",
                ),
            )
        operation = {"pause": self.pause, "resume": self.resume, "cancel": self.cancel}[action]
        try:
            result = operation(task_id)
            state = RuntimeState.TIMEOUT if result.new_status == TaskStatus.TIMED_OUT else RuntimeState(result.new_status.value)
            failure = None if result.success else RuntimeFailure("control_rejected", result.reason or "control rejected", category="unauthorized_control")
            return self._governed_response(request, task_id, state, failure=failure)
        except RufloAdapterError as exc:
            return self._governed_response(
                request,
                task_id,
                RuntimeState.FAILED,
                failure=RuntimeFailure(exc.category, str(exc), category="unavailable", retryable=True),
            )

    def runtime_result(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        try:
            result = self.result(task_id)
            state = RuntimeState.TIMEOUT if result.status == TaskStatus.TIMED_OUT else RuntimeState(result.status.value)
            failure = None if result.verification_passed else RuntimeFailure("verification_required", "required verification did not pass", category="verification")
            verification_status = "passed" if result.verification_passed else "failed"
            return self._governed_response(
                request,
                task_id,
                state,
                output={"artifacts": result.output_artifacts},
                failure=failure,
                metadata={"verification_status": verification_status},
            )
        except RufloAdapterError as exc:
            return self._governed_response(
                request,
                task_id,
                RuntimeState.FAILED,
                failure=RuntimeFailure(exc.category, str(exc), category="unavailable", retryable=True),
            )

    def runtime_verify(self, request: RuntimeRequest, task_id: str) -> RuntimeResponse:
        result = self.runtime_result(request, task_id)
        if result.failure is not None:
            return result
        state = (
            result.state if isinstance(result.state, RuntimeState) else RuntimeState(result.state)
        )
        return self._governed_response(
            request,
            task_id,
            state,
            output=result.output,
            metadata={**dict(result.metadata), "verified": True, "verification_status": "passed"},
        )

    def health_check(self) -> dict[str, Any]:
        """Health check endpoint for adapter."""
        return {
            "status": "fake" if self.config.fake_mode else "live",
            "protocol_version": RUFLO_ADAPTER_PROTOCOL_VERSION,
            "fake_mode": self.config.fake_mode,
            "capabilities": {
                "required": self._capability_manifest.required,
                "optional": self._capability_manifest.optional,
            },
            "trust_boundaries": {
                "trust_protected_work": self.config.trust_protected_work,
                "require_verification_for_protected": self.config.require_verification_for_protected,
            },
        }

    def validate_capability_manifest(self, manifest: CapabilityManifest) -> tuple[bool, list[str]]:
        """Validate a capability manifest against adapter capabilities."""
        issues = []
        available = set(self._capability_manifest.required + self._capability_manifest.optional)

        for req in manifest.required:
            if req not in available:
                issues.append(f"Required capability not available: {req}")

        for req in manifest.optional:
            if req not in available:
                issues.append(f"Optional capability not available: {req}")

        for forbidden in manifest.forbidden:
            if forbidden in available:
                issues.append(f"Forbidden capability is available: {forbidden}")

        return len(issues) == 0, issues

    def _control(
        self,
        action: TaskAction,
        task_id: str,
        workflow_id: str | None = None,
        reason: str = "",
        approver: str | None = None,
        replan_spec: dict[str, Any] | None = None,
    ) -> RufloControlResponse:
        """Execute a control action."""

        request = RufloControlRequest(
            task_id=task_id,
            action=action,
            workflow_id=workflow_id,
            reason=reason,
            approver=approver,
            replan_spec=replan_spec,
        )

        if self.config.fake_mode or self._transport is None:
            return self._fake_control(request)

        try:
            response_data = self._dispatch("control", request.__dict__)
            return RufloControlResponse.from_dict(response_data)
        except Exception as e:
            raise RufloUnavailableError(f"Control action {action.value} failed: {e}") from e

    # ===== Fake Adapter Implementation (Deterministic Testing) =====

    def _fake_submit(self, request: RufloSubmitRequest) -> RufloSubmitResponse:
        """Fake submit for testing - simulates acceptance and queuing."""
        task_id = f"fake-task-{uuid.uuid4().hex[:8]}"
        workflow_id = f"fake-workflow-{uuid.uuid4().hex[:8]}" if request.workflow_plan else None

        # Simulate latency
        if self.config.fake_latency_ms > 0:
            time.sleep(self.config.fake_latency_ms / 1000.0)

        # Store in fake state
        steps = (
            [s.get("action", "unknown") for s in request.workflow_plan.get("steps", [])]
            if request.workflow_plan
            else []
        )
        self._fake_state[task_id] = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "status": TaskStatus.QUEUED,
            "request": request.to_dict(),
            "started_at": None,
            "completed_at": None,
            "progress_pct": 0.0,
            "current_step": None,
            "steps_completed": [],
            "steps_pending": steps,
            "steps_failed": [],
            "cost_usd": 0.0,
            "tokens_used": 0,
            "verification_results": [],
            "error": None,
            "replan_count": 0,
            "status_check_count": 0,  # Track status checks for controlled progression
        }

        return RufloSubmitResponse(
            task_id=task_id,
            workflow_id=workflow_id,
            status=TaskStatus.QUEUED,
            accepted=True,
            reason="Accepted for execution",
            estimated_start_at=datetime.now(timezone.utc).isoformat(),
        )

    def _fake_status(self, request: RufloStatusRequest) -> RufloStatusResponse:
        """Fake status query with controlled progression."""
        task_data = self._fake_state.get(request.task_id)
        if not task_data:
            return RufloStatusResponse(
                task_id=request.task_id,
                workflow_id=request.workflow_id,
                status=TaskStatus.REJECTED,
                error="Task not found",
            )

        # Increment status check counter
        task_data["status_check_count"] = task_data.get("status_check_count", 0) + 1
        check_count = task_data["status_check_count"]

        # Simulate progress for queued/running tasks
        status = task_data["status"]
        if status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            # First check: stay QUEUED
            if status == TaskStatus.QUEUED and check_count == 1:
                pass  # Stay QUEUED on first check
            # Second check: transition to RUNNING
            elif status == TaskStatus.QUEUED and check_count == 2:
                task_data["status"] = TaskStatus.RUNNING
                task_data["started_at"] = datetime.now(timezone.utc).isoformat()
                task_data["current_step"] = (
                    task_data["steps_pending"][0] if task_data["steps_pending"] else "execution"
                )
            # Third check: 25% progress
            elif status == TaskStatus.RUNNING and check_count == 3:
                task_data["progress_pct"] = 25.0
            # Fourth check: 50% progress
            elif status == TaskStatus.RUNNING and check_count == 4:
                task_data["progress_pct"] = 50.0
            # Fifth check: 100% complete
            elif status == TaskStatus.RUNNING and check_count >= 5:
                task_data["status"] = TaskStatus.COMPLETED
                task_data["completed_at"] = datetime.now(timezone.utc).isoformat()
                task_data["current_step"] = None
                task_data["steps_completed"] = task_data["steps_pending"]
                task_data["steps_pending"] = []
                task_data["verification_results"] = [{"check": "tests", "outcome": "pass"}]
                task_data["progress_pct"] = 100.0

        return RufloStatusResponse(
            task_id=task_data["task_id"],
            workflow_id=task_data["workflow_id"],
            status=task_data["status"],
            progress_pct=task_data["progress_pct"],
            current_step=task_data["current_step"],
            steps_completed=task_data["steps_completed"],
            steps_pending=task_data["steps_pending"],
            steps_failed=task_data["steps_failed"],
            started_at=task_data["started_at"],
            completed_at=task_data["completed_at"],
            cost_usd=task_data["cost_usd"],
            tokens_used=task_data["tokens_used"],
            verification_results=task_data["verification_results"],
            error=task_data["error"],
        )

    def _fake_control(self, request: RufloControlRequest) -> RufloControlResponse:
        """Fake control action."""
        task_data = self._fake_state.get(request.task_id)
        if not task_data:
            return RufloControlResponse(
                task_id=request.task_id,
                action=request.action,
                success=False,
                previous_status=TaskStatus.REJECTED,
                new_status=TaskStatus.REJECTED,
                reason="Task not found",
            )

        previous_status = task_data["status"]

        if request.action == TaskAction.PAUSE:
            if previous_status == TaskStatus.RUNNING:
                task_data["status"] = TaskStatus.PAUSED
                new_status = TaskStatus.PAUSED
                success = True
            else:
                success = False
                new_status = previous_status

        elif request.action == TaskAction.RESUME:
            if previous_status == TaskStatus.PAUSED:
                task_data["status"] = TaskStatus.RUNNING
                new_status = TaskStatus.RUNNING
                success = True
            else:
                success = False
                new_status = previous_status

        elif request.action == TaskAction.CANCEL:
            if previous_status in (
                TaskStatus.PENDING,
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
            ):
                task_data["status"] = TaskStatus.CANCELLED
                task_data["completed_at"] = datetime.now(timezone.utc).isoformat()
                task_data["error"] = request.reason or "Cancelled by user"
                new_status = TaskStatus.CANCELLED
                success = True
            else:
                success = False
                new_status = previous_status

        elif request.action == TaskAction.APPROVE:
            if previous_status == TaskStatus.WAITING_APPROVAL:
                task_data["status"] = TaskStatus.RUNNING
                new_status = TaskStatus.RUNNING
                success = True
            else:
                success = False
                new_status = previous_status

        elif request.action == TaskAction.REJECT:
            if previous_status == TaskStatus.WAITING_APPROVAL:
                task_data["status"] = TaskStatus.REJECTED
                task_data["completed_at"] = datetime.now(timezone.utc).isoformat()
                task_data["error"] = request.reason or "Rejected by approver"
                new_status = TaskStatus.REJECTED
                success = True
            else:
                success = False
                new_status = previous_status

        elif request.action == TaskAction.REPLAN:
            if previous_status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.REJECTED):
                task_data["status"] = TaskStatus.QUEUED
                task_data["progress_pct"] = 0.0
                task_data["started_at"] = None
                task_data["completed_at"] = None
                task_data["current_step"] = None
                task_data["steps_completed"] = []
                task_data["steps_failed"] = []
                task_data["status_check_count"] = 0  # Reset for fresh progression
                task_data["replan_count"] = task_data.get("replan_count", 0) + 1
                new_status = TaskStatus.QUEUED
                success = True
            else:
                success = False
                new_status = previous_status

        else:
            success = False
            new_status = previous_status

        return RufloControlResponse(
            task_id=request.task_id,
            action=request.action,
            success=success,
            previous_status=previous_status,
            new_status=new_status,
            reason="" if success else f"Cannot {request.action.value} from {previous_status.value}",
        )

    def _fake_result(self, task_id: str, workflow_id: str | None = None) -> RufloResult:
        """Fake result retrieval."""
        task_data = self._fake_state.get(task_id)
        if not task_data:
            return RufloResult(
                task_id=task_id,
                workflow_id=workflow_id,
                status=TaskStatus.REJECTED,
                outcome="failure",
                error="Task not found",
            )

        # Ensure task is completed
        while task_data["status"] not in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.REJECTED,
        ):
            self._fake_status(RufloStatusRequest(task_id=task_id))

        return RufloResult(
            task_id=task_data["task_id"],
            workflow_id=task_data["workflow_id"],
            status=task_data["status"],
            outcome="success" if task_data["status"] == TaskStatus.COMPLETED else "failure",
            output_artifacts=[],
            output_data={},
            verification_passed=task_data["status"] == TaskStatus.COMPLETED,
            verification_results=task_data["verification_results"],
            cost_usd=task_data["cost_usd"],
            tokens_used=task_data["tokens_used"],
            latency_ms=0,
            started_at=task_data["started_at"],
            completed_at=task_data["completed_at"],
            error=task_data["error"],
            replan_count=task_data.get("replan_count", 0),
        )


def build_ruflo_adapter(
    config: RufloAdapterConfig | None = None,
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> RufloAdapter:
    """Factory function for creating a Ruflo adapter."""
    return RufloAdapter(config=config, transport=transport)


def build_fake_ruflo_adapter(
    fake_latency_ms: int = 10, fake_failure_rate: float = 0.0, **config_kwargs: Any
) -> RufloAdapter:
    """Factory for creating a fake Ruflo adapter for testing."""
    config = RufloAdapterConfig(
        fake_mode=True,
        fake_latency_ms=fake_latency_ms,
        fake_failure_rate=fake_failure_rate,
        **config_kwargs,
    )
    return RufloAdapter(config=config)


__all__ = [
    "RUFLO_ADAPTER_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "CapabilityManifest",
    "RufloAdapter",
    "RufloAdapterConfig",
    "RufloAdapterError",
    "RufloApprovalError",
    "RufloCancellationError",
    "RufloCapacityError",
    "RufloControlRequest",
    "RufloControlResponse",
    "RufloProtocolError",
    "RufloResult",
    "RufloStatusRequest",
    "RufloStatusResponse",
    "RufloSubmitRequest",
    "RufloSubmitResponse",
    "RufloTimeoutError",
    "RufloUnavailableError",
    "RufloValidationError",
    "RufloVerificationError",
    "TaskAction",
    "TaskStatus",
    "build_fake_ruflo_adapter",
    "build_ruflo_adapter",
    "negotiate_protocol_version",
]
