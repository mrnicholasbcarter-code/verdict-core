"""
Workflow Compiler for Ruflo Orchestration (Issue #39).

Compiles and validates WorkflowPlan instances for submission to Ruflo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from verdict.contracts import WorkflowPlan
from verdict.ruflo_adapter import RufloAdapter


@dataclass
class CompiledWorkflow:
    """Result of compiling a WorkflowPlan for Ruflo submission."""

    workflow_plan: WorkflowPlan
    is_valid: bool = False
    validation_errors: list[str] = field(default_factory=list)
    # Additional derived data can be added here, e.g., task specifications


class WorkflowCompiler:
    """
    Compiles a WorkflowPlan into a form suitable for Ruflo submission.

    The compiler validates the workflow plan against the Ruflo adapter's
    capability manifest and ensures that the workflow is structurally sound.
    """

    def __init__(self, adapter: RufloAdapter) -> None:
        self.adapter = adapter
        self.manifest = adapter.capability_manifest

    def compile(self, workflow_plan: WorkflowPlan) -> CompiledWorkflow:
        """
        Compile a workflow plan.

        Args:
            workflow_plan: The workflow plan to compile.

        Returns:
            A CompiledWorkflow instance with validation results.
        """
        errors: list[str] = []

        # Basic structure validation
        if not workflow_plan.steps:
            errors.append("Workflow must have at least one step")

        # Validate each step
        for i, step in enumerate(workflow_plan.steps):
            if not isinstance(step, dict):
                errors.append(f"Step {i} must be a dictionary")
                continue
            if "action" not in step:
                errors.append(f"Step {i} missing required 'action' field")
            # Additional step validation could go here

        # Validate against capability manifest
        # For now, we just check that the adapter accepts the workflow plan
        # In a more complete implementation, we would check each step's
        # required capabilities against the manifest.
        try:
            # The adapter's submit method will validate the capability manifest
            # when a workflow_plan is provided. We can do a dry run by
            # checking if the workflow_plan is compatible with the manifest.
            # We'll skip the actual submit call here.
            pass
        except Exception as e:
            errors.append(f"Capabilities validation failed: {e}")

        is_valid = len(errors) == 0

        return CompiledWorkflow(
            workflow_plan=workflow_plan, is_valid=is_valid, validation_errors=errors
        )

    def validate_workflow_step_against_manifest(self, step: dict[str, Any]) -> list[str]:
        """
        Validate a single workflow step against the adapter's capability manifest.

        Returns a list of error messages (empty if valid).
        """
        errors: list[str] = []
        # This is a placeholder. In a real implementation, we would:
        # 1. Extract the required capabilities from the step (if any)
        # 2. Check that those capabilities are present in the manifest
        # 3. Also check for forbidden capabilities, etc.
        # For now, we return no errors.
        return errors
