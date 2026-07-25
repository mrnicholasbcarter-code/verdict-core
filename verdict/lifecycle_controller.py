"""
Lifecycle Controller for Ruflo Orchestration (Issue #40).

Manages the lifecycle of a workflow using the RufloAdapter.
Integrates workflow compilation and verification gates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from verdict.ruflo_adapter import (
    RufloAdapter,
)
from verdict.workflow_compiler import CompiledWorkflow, WorkflowCompiler


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
        task_spec: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> WorkflowHandle:
        """
        Submit a workflow for execution.

        Args:
            workflow_plan: The workflow to execute.
            task_spec: The task specification (as dict) for the workflow.
            idempotency_key: Optional idempotency key for deduplication.

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
            raise ValueError(
                f"Workflow validation failed: {', '.join(compiled.validation_errors)}"
            )

        # Submit to Ruflo
        submit_response: any = self.adapter.submit(
            task_spec=task_spec,
            workflow_plan=workflow_plan,
            idempotency_key=idempotency_key,
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
            self._run_verification_hooks(workflow_plan, stage="submit")

        return handle

    def status(self, workflow_id: str) -> Any | None:
        """
        Get the current status of a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            The status response, or None if not found.
        """
        handle = self._workflows.get(workflow_id)
        if not handle:
            return None

        status_response: any = self.adapter.status(
            task_id=handle.task_id,
            workflow_id=workflow_id,
        )
        handle.last_status = status_response

        # If the workflow is complete, fetch the result
        if getattr(status_response, 'status', None) in (
            "completed",
            "failed",
            "cancelled",
            "rejected",
            "timed_out",
        ) and not handle.result:
            result: any = self.adapter.result(
                task_id=handle.task_id,
                workflow_id=workflow_id,
            )
            handle.result = result

            # Optionally run verification on completion
            if self.config.run_verification_on_complete:
                self._run_verification_hooks(
                    workflow_plan=handle.workflow_plan,
                    stage="complete",
                )

        return status_response

    def wait_for_completion(
        self, workflow_id: str, timeout: float = 300.0
    ) -> Any | None:
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
            if getattr(status, 'status', None) in (
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
        response: any = self.adapter.pause(
            task_id=handle.task_id, workflow_id=workflow_id
        )
        return getattr(response, 'success', False)

    def resume(self, workflow_id: str) -> bool:
        """Resume a paused workflow."""
        handle = self._workflows.get(workflow_id)
        if not handle:
            return False
        response: any = self.adapter.resume(
            task_id=handle.task_id, workflow_id=workflow_id
        )
        return getattr(response, 'success', False)

    def cancel(self, workflow_id: str, reason: str = "") -> bool:
        """Cancel a workflow."""
        handle = self._workflows.get(workflow_id)
        if not handle:
            return False
        response: any = self.adapter.cancel(
            task_id=handle.task_id, workflow_id=workflow_id, reason=reason
        )
        if getattr(response, 'success', False):
            # Remove from active tracking? Or keep for history.
            pass
        return getattr(response, 'success', False)

    def _run_verification_hooks(
        self, workflow_plan: Any, stage: str
    ) -> list[Any]:
        """
        Run the configured verification gates for a workflow at a given stage.

        Returns a list of validation results.
        """
        results: list[Any] = []

        # 1. Run verification gates from the workflow plan (if enabled)
        if self.config.use_workflow_plan_verification and hasattr(workflow_plan, 'verification'):
            verification = getattr(workflow_plan, 'verification', None)
            if verification:
                # If it's a dict, we might need to convert it to a VerificationPlan
                # For simplicity, we treat it as a VerificationPlan-like object.
                # In a real implementation, we would instantiate the checks and run them.
                # We'll create a placeholder result indicating we found verification.
                results.append(
                    {
                        "stage": stage,
                        "source": "workflow_plan",
                        "passed": True,  # placeholder - actual evaluation would go here
                        "message": "Verification from workflow plan (placeholder)",
                        "details": str(verification)[:200],  # truncate for brevity
                    }
                )

        # 2. Run any extra verification gates configured in the LifecycleConfig
        for gate in self.config.extra_verification_gates:
            # Each gate should have an evaluate method that returns a result
            if hasattr(gate, 'evaluate'):
                try:
                    result = gate.evaluate(workflow_plan=workflow_plan, stage=stage)
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
            else:
                results.append(
                    {
                        "stage": stage,
                        "source": "extra_gate",
                        "passed": False,
                        "message": "Invalid verification gate (no evaluate method)",
                    }
                )

        return results
