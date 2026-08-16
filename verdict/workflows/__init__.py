"""Workflow orchestration contracts and implementations (issue #259).

The autodev package provides the 12-stage autonomous software-development
workflow: Discovery, Repository Analysis, Memory Lookup, Research,
Architecture, Atomic Work Slices, Model Assignment, Delegated Execution,
Review, Verification, Learning, and Release.

Stages are orchestration units.  They validate transitions through
:class:`verdict.eligibility.EligibilityGate` and ``BeforeExecution`` hook
evaluation, and they require an evidence receipt before a stage may
transition to the next.
"""

from verdict.workflows.autodev import (
    AUTO_DEV_STAGES,
    STAGE_NAMES,
    AutoDevWorkflow,
    BeforeExecutionHook,
    ExecutionStrategyKind,
    Stage,
    StageContext,
    StageResult,
    StageStatus,
    Workflow,
    WorkflowGate,
    WorkflowRunResult,
    WorkflowTransitionError,
    WorkflowVerificationError,
    make_evidence_receipt,
)

__all__ = [
    "AUTO_DEV_STAGES",
    "STAGE_NAMES",
    "AutoDevWorkflow",
    "BeforeExecutionHook",
    "ExecutionStrategyKind",
    "Stage",
    "StageContext",
    "StageResult",
    "StageStatus",
    "Workflow",
    "WorkflowGate",
    "WorkflowRunResult",
    "WorkflowTransitionError",
    "WorkflowVerificationError",
    "make_evidence_receipt",
]
