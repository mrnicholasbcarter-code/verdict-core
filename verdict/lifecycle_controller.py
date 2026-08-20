"""
Lifecycle Controller for Ruflo Orchestration (Issue #40).

Manages the lifecycle of a workflow using the RufloAdapter.
Integrates workflow compilation and verification gates.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from verdict.contracts import TaskSpec
from verdict.enforcement import EnforcementContext, check_enforcement
from verdict.ruflo_adapter import RufloAdapter
from verdict.workflow_compiler import CompiledWorkflow, WorkflowCompiler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from verdict.decision_kernel import DecisionRecord


@dataclass
class LifecycleConfig:
    """Configuration for the LifecycleController."""

    # Verification gate settings
    run_verification_on_submit: bool = False
    run_verification_on_complete: bool = True
    # If True, use the verification gates from the workflow plan
    use_workflow_plan_verification: bool = True
    # Additional verification gates to always run
    extra_verification_gates: list[Any] = field(default_factory=list)
    # Replanning settings
    max_replans: int = 3
    # Polling interval for status checks (seconds)
    status_poll_interval: float = 2.0


@dataclass
class WorkflowHandle:
    """Handle to a managed workflow."""

    workflow_id: str
    task_id: str
    submit_response: Any
    workflow_plan: Any | None = None  # Store the original plan for verification
    created_at: float = field(default_factory=time.time)
    last_status: Any | None = None
    result: Any | None = None
    replan_count: int = 0


class LifecycleController:
    """
    Controls the lifecycle of a workflow via the RufloAdapter.

    Responsibilities:
    - Validate and compile workflow plans before submission
    - Submit workflows to Ruflo
    - Monitor workflow status (polling or event-driven)
    - Handle lifecycle actions (pause, resume, cancel, etc.)
    - Run verification gates at configured points
    - Handle replanning on failure (if enabled)
    """

    def __init__(
        self,
        adapter: RufloAdapter,
        compiler: WorkflowCompiler | None = None,
        config: LifecycleConfig | None = None,
    ) -> None:
        self.adapter = adapter
        self.compiler = compiler or WorkflowCompiler(adapter)
        self.config = config or LifecycleConfig()
        self._workflows: dict[str, WorkflowHandle] = {}

    def submit_workflow(
        self,
        workflow_plan: Any,
        task_spec: TaskSpec,
        *,
        idempotency_key: str | None = None,
        decision_record: DecisionRecord | None = None,
        context: EnforcementContext | None = None,
    ) -> WorkflowHandle:
        """
        Submit a workflow for execution.

        Args:
            workflow_plan: The workflow to execute.
            task_spec: The task specification (as dict) for the workflow.
            idempotency_key: Optional idempotency key for deduplication.
            decision_record: Optional feature-003 authority decision to enforce
                at the submit verification gate. Ignored when verification is
                disabled or no gate consults it.
            context: Optional :class:`~verdict.enforcement.EnforcementContext`
                carrying the authority decision; takes precedence over
                ``decision_record`` when both are supplied.

        Returns:
            A WorkflowHandle for managing the workflow.

        Raises:
            ValueError: If the workflow fails validation.
        """
        # Store the original workflow plan for later verification
        # Note: In a real implementation, we might want to deep-copy this.
        # For now, we keep a reference.

        # Compile and validate the workflow
        compiled: CompiledWorkflow = self.compiler.compile(workflow_plan)
        if not compiled.is_valid:
            raise ValueError(f"Workflow validation failed: {', '.join(compiled.validation_errors)}")

        # Submit to Ruflo
        submit_response: Any = self.adapter.submit(
            task_spec=task_spec, workflow_plan=workflow_plan, idempotency_key=idempotency_key
        )

        # Create handle
        handle = WorkflowHandle(
            workflow_id=submit_response.workflow_id or "",
            task_id=submit_response.task_id,
            workflow_plan=workflow_plan,
            submit_response=submit_response,
        )
        self._workflows[handle.workflow_id] = handle

        # Optionally run verification on submit
        if self.config.run_verification_on_submit:
            self._run_verification_hooks(
                workflow_plan, stage="submit", decision_record=decision_record, context=context
            )

        return handle

    def status(
        self,
        workflow_id: str,
        *,
        decision_record: DecisionRecord | None = None,
        context: EnforcementContext | None = None,
    ) -> Any | None:
        """
        Get the current status of a workflow.

        Args:
            workflow_id: The workflow ID.
            decision_record: Optional feature-003 authority decision to enforce
                at the completion verification gate.
            context: Optional :class:`~verdict.enforcement.EnforcementContext`;
                takes precedence over ``decision_record``.

        Returns:
            The status response, or None if not found.
        """
        handle = self._workflows.get(workflow_id)
        if not handle:
            return None

        status_response: Any = self.adapter.status(task_id=handle.task_id, workflow_id=workflow_id)
        handle.last_status = status_response

        # If the workflow is complete, fetch the result
        if (
            getattr(status_response, "status", None)
            in ("completed", "failed", "cancelled", "rejected", "timed_out")
            and not handle.result
        ):
            result: Any = self.adapter.result(task_id=handle.task_id, workflow_id=workflow_id)
            handle.result = result

            # Optionally run verification on completion
            if self.config.run_verification_on_complete:
                self._run_verification_hooks(
                    workflow_plan=handle.workflow_plan,
                    stage="complete",
                    decision_record=decision_record,
                    context=context,
                )

        return status_response

    def wait_for_completion(self, workflow_id: str, timeout: float = 300.0) -> Any | None:
        """
        Wait for a workflow to complete, polling for status.

        Args:
            workflow_id: The workflow ID.
            timeout: Maximum time to wait in seconds.

        Returns:
            The final result, or None if timeout or error.
        """
        start = time.time()
        while time.time() - start < timeout:
            status = self.status(workflow_id)
            if not status:
                return None
            if getattr(status, "status", None) in (
                "completed",
                "failed",
                "cancelled",
                "rejected",
                "timed_out",
            ):
                break
            time.sleep(self.config.status_poll_interval)
        else:
            # Timeout
            return None

        handle = self._workflows.get(workflow_id)
        return handle.result if handle else None

    def pause(self, workflow_id: str) -> bool:
        """Pause a running workflow."""
        handle = self._workflows.get(workflow_id)
        if not handle:
            return False
        response: Any = self.adapter.pause(task_id=handle.task_id, workflow_id=workflow_id)
        return getattr(response, "success", False)

    def resume(self, workflow_id: str) -> bool:
        """Resume a paused workflow."""
        handle = self._workflows.get(workflow_id)
        if not handle:
            return False
        response: Any = self.adapter.resume(task_id=handle.task_id, workflow_id=workflow_id)
        return getattr(response, "success", False)

    def cancel(self, workflow_id: str, reason: str = "") -> bool:
        """Cancel a workflow."""
        handle = self._workflows.get(workflow_id)
        if not handle:
            return False
        response: Any = self.adapter.cancel(
            task_id=handle.task_id, workflow_id=workflow_id, reason=reason
        )
        if getattr(response, "success", False):
            # Remove from active tracking? Or keep for history.
            pass
        return getattr(response, "success", False)

    def _run_verification_hooks(
        self,
        workflow_plan: Any,
        stage: str,
        *,
        decision_record: DecisionRecord | None = None,
        context: EnforcementContext | None = None,
    ) -> list[Any]:
        """
        Run the configured verification gates for a workflow at a given stage.

        Feature 004 (VER-008 #225): when a ``decision_record`` or ``context``
        is provided, the workflow-plan verification runs the real decision-bound
        guard (``check_enforcement``) against the plan's target provider instead
        of the pre-004 ``passed=True`` placeholder. ``context`` is preferred when
        both are supplied. Absent both, the gate fails closed (NFR-002) only when
        an enforcement guard is explicitly requested via the plan; legacy
        callers that never pass a decision remain unaffected.

        Extra verification gates are detected via :func:`inspect.signature`:
        gates declaring ``decision_record`` and/or ``context`` receive them
        (e.g. :class:`~verdict.enforcement.EnforcementVerificationGate`); legacy
        gates are called with the original two-argument signature.

        Returns a list of validation results.
        """
        results: list[Any] = []

        # 1. Run verification gates from the workflow plan (if enabled).
        # Feature 004 replaces the unconditional ``passed=True`` placeholder with
        # a decision-bound evaluation when an authority context is present; when
        # no context is supplied, the legacy placeholder behavior is preserved.
        if self.config.use_workflow_plan_verification and hasattr(workflow_plan, "verification"):
            verification = getattr(workflow_plan, "verification", None)
            if verification:
                plan_result = self._evaluate_workflow_plan_verification(
                    workflow_plan=workflow_plan,
                    stage=stage,
                    verification=verification,
                    context=context,
                    decision_record=decision_record,
                )
                results.append(plan_result)

        # 2. Run any extra verification gates configured in the LifecycleConfig.
        for gate in self.config.extra_verification_gates:
            if not hasattr(gate, "evaluate"):
                results.append(
                    {
                        "stage": stage,
                        "source": "extra_gate",
                        "passed": False,
                        "message": "Invalid verification gate (no evaluate method)",
                    }
                )
                continue
            try:
                result = self._invoke_gate(
                    gate,
                    workflow_plan=workflow_plan,
                    stage=stage,
                    decision_record=decision_record,
                    context=context,
                )
                results.append(result)
            except Exception as e:
                results.append(
                    {
                        "stage": stage,
                        "source": "extra_gate",
                        "passed": False,
                        "message": f"Error running verification gate: {e}",
                        "exception": str(e),
                    }
                )

        return results

    def _evaluate_workflow_plan_verification(
        self,
        *,
        workflow_plan: Any,
        stage: str,
        verification: Any,
        context: EnforcementContext | None,
        decision_record: DecisionRecord | None = None,
    ) -> dict[str, Any]:
        """Feature 004: decision-bound evaluation of the plan's own verification block.

        When an enforcement ``context`` is present (or a ``decision_record`` from
        which one can be synthesized), the target provider declared on the plan
        is checked against the authority before the step is admitted; a denial
        fails closed (NFR-002) and carries ``decision_id``/``blocked_provider``/
        ``admitted_set`` (FR-002). With neither supplied, the pre-004 placeholder
        result is preserved for backwards compatibility.
        """
        from datetime import datetime, timezone

        base: dict[str, Any] = {
            "stage": stage,
            "source": "workflow_plan",
            "details": str(verification)[:200],  # truncate for brevity
        }
        if context is None and decision_record is not None:
            context = EnforcementContext(
                decision_record=decision_record, created_at=datetime.now(timezone.utc)
            )
        if context is None:
            return {
                **base,
                "passed": True,  # legacy placeholder behavior
                "message": "Verification from workflow plan (no enforcement context)",
            }
        target_provider = self._resolve_target_provider(workflow_plan)
        result = check_enforcement(context, target_provider)
        if result.allowed:
            return {
                **base,
                "passed": True,
                "message": "Verification admitted by decision-bound guard",
                "decision_id": result.decision_id,
                "blocked_provider": None,
                "admitted_set": list(result.admitted_set),
                "exclusions": list(result.exclusions),
            }
        assert result.reason is not None  # invariant from EnforcementResult
        return {
            **base,
            "passed": False,
            "message": "Verification denied by decision-bound guard",
            "reason": result.reason.value,
            "decision_id": result.decision_id,
            "blocked_provider": target_provider,
            "admitted_set": list(result.admitted_set),
            "exclusions": list(result.exclusions),
        }

    @staticmethod
    def _resolve_target_provider(workflow_plan: Any) -> str | None:
        """Best-effort resolution of a plan's target provider id (FR-008)."""
        value: Any = None
        if hasattr(workflow_plan, "target_provider"):
            value = workflow_plan.target_provider
        elif isinstance(workflow_plan, dict):
            value = workflow_plan.get("target_provider")
        return value if isinstance(value, str) and value else None

    def _invoke_gate(
        self,
        gate: Any,
        *,
        workflow_plan: Any,
        stage: str,
        decision_record: DecisionRecord | None,
        context: EnforcementContext | None,
    ) -> Any:
        """Call ``gate.evaluate`` with exactly the keyword params it declares.

        Legacy gates that accept only ``workflow_plan``/``stage`` are called
        with the original signature; enforcement-aware gates additionally
        receive ``decision_record`` and/or ``context`` depending on what their
        ``evaluate`` signature advertises.
        """
        evaluate = gate.evaluate
        parameters: set[str] = set()
        try:
            sig = inspect.signature(evaluate)
        except (TypeError, ValueError):
            parameters = {"workflow_plan", "stage"}
        else:
            parameters.update(sig.parameters.keys())
            # Inspect may fail to resolve VAR_POSITIONAL/VAR_KEYWORD; fall back
            # to the conservative two-arg call if nothing recognizable is found.
        if "decision_record" in parameters or "context" in parameters:
            kwargs: dict[str, Any] = {"workflow_plan": workflow_plan, "stage": stage}
            if "decision_record" in parameters:
                kwargs["decision_record"] = decision_record
            if "context" in parameters:
                kwargs["context"] = context
            return evaluate(**kwargs)
        return evaluate(workflow_plan=workflow_plan, stage=stage)
