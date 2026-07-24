"""
Ruflo verification gates and bounded replanning (Issue #41 / Slice 32.4).

This module provides:
- Typed verifier node/result contract with pass, fail, blocked, inconclusive outcomes
- Verification required before success for protected/explicitly verified tasks
- Failure classification: provider, quota, tool, permission, test, plan, unknown
- Bounded replanning: max replans, budget/concurrency/permissions/risk floor preservation
- Approval requirements for destructive/credential/production/deployment remediation
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class VerificationOutcome(enum.Enum):
    """Possible outcomes of a verification check."""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"


class FailureClassification(enum.Enum):
    """Classification of verification failures."""

    PROVIDER = "provider"  # Provider API error, rate limit, unavailable
    QUOTA = "quota"  # Budget/token quota exceeded
    TOOL = "tool"  # Tool execution failed (missing, crashed, timeout)
    PERMISSION = "permission"  # Permission denied, auth failure
    TEST = "test"  # Test suite failure
    PLAN = "plan"  # Plan invalid, missing steps, circular deps
    UNKNOWN = "unknown"  # Unclassified failure


class ReplanReason(enum.Enum):
    """Reason for a replan attempt."""

    VERIFICATION_FAILED = "verification_failed"
    QUOTA_EXCEEDED = "quota_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TOOL_FAILURE = "tool_failure"
    APPROVAL_DENIED = "approval_denied"
    PARTIAL_FAILURE = "partial_failure"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class VerificationEvidence:
    """Evidence reference for a verification check."""

    check_name: str
    command: str
    exit_code: int
    stdout_ref: str  # Content-addressed reference
    stderr_ref: str
    duration_ms: int
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    versions: dict[str, str] = field(default_factory=dict)  # tool -> version


@dataclass(frozen=True)
class VerificationResult:
    """Result of a single verification check."""

    check_name: str
    outcome: VerificationOutcome
    classification: FailureClassification | None = None
    message: str = ""
    evidence: VerificationEvidence | None = None
    requires_approval: bool = False
    approval_reason: str = ""


@dataclass(frozen=True)
class VerificationGate:
    """Configuration for a verification gate."""

    name: str
    required: bool = True
    command: str = ""
    timeout_seconds: int = 60
    allowed_failures: int = 0  # For partial failure tolerance
    requires_approval_on_fail: bool = False
    approval_reason: str = ""


@dataclass
class ReplanRecord:
    """Record of a replan attempt."""

    replan_id: str = field(
        default_factory=lambda: hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
    )
    attempt: int = 0
    reason: ReplanReason = ReplanReason.VERIFICATION_FAILED
    original_plan_hash: str = ""
    new_plan_hash: str = ""
    budget_delta_usd: float = 0.0
    concurrency_delta: int = 0
    permission_delta: list[str] = field(default_factory=list)
    risk_floor_delta: float = 0.0
    approved: bool = False
    approver: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class VerificationGateContext:
    """Context for running verification gates."""

    task_id: str
    attempt_id: str
    verification_gates: list[VerificationGate] = field(default_factory=list)
    max_replans: int = 3
    budget_usd: float = 100.0
    max_concurrency: int = 5
    required_permissions: set[str] = field(default_factory=set)
    risk_floor: float = 0.0
    replan_records: list[ReplanRecord] = field(default_factory=list)
    current_plan_hash: str = ""
    redacted_evidence: dict[str, str] = field(default_factory=dict)  # ref -> redacted content


def run_verification_gates(
    context: VerificationGateContext,
) -> tuple[bool, list[VerificationResult]]:
    """
    Run all verification gates for a task.

    Returns:
        (all_passed, results)
    """
    import subprocess

    results = []
    all_passed = True

    for gate in context.verification_gates:
        if not gate.command:
            result = VerificationResult(
                check_name=gate.name,
                outcome=VerificationOutcome.INCONCLUSIVE,
                classification=FailureClassification.PLAN,
                message="No command specified for verification gate",
            )
            results.append(result)
            if gate.required:
                all_passed = False
            continue

        start = time.time()
        try:
            proc = subprocess.run(
                gate.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=gate.timeout_seconds,
            )
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as e:
            exit_code = -1
            stdout = e.stdout.decode() if e.stdout else ""
            stderr = (
                f"Timeout after {gate.timeout_seconds}s: {e.stderr.decode() if e.stderr else ''}"
            )
        except Exception as e:
            exit_code = -1
            stdout = ""
            stderr = str(e)

        duration_ms = int((time.time() - start) * 1000)

        # Determine outcome
        if exit_code == 0:
            outcome = VerificationOutcome.PASS
        else:
            outcome = VerificationOutcome.FAIL
            all_passed = False

        # Classify failure
        classification = None
        if outcome == VerificationOutcome.FAIL:
            stderr_lower = stderr.lower()
            if any(kw in stderr_lower for kw in ["rate limit", "quota", "budget", "token limit"]):
                classification = FailureClassification.QUOTA
            elif any(
                kw in stderr_lower
                for kw in ["unauthorized", "permission denied", "forbidden", "auth"]
            ):
                classification = FailureClassification.PERMISSION
            elif any(
                kw in stderr_lower
                for kw in ["tool not found", "command not found", "timeout", "crash"]
            ):
                classification = FailureClassification.TOOL
            elif any(kw in stderr_lower for kw in ["test failed", "assertion", "pytest", "jest"]):
                classification = FailureClassification.TEST
            elif any(
                kw in stderr_lower for kw in ["plan", "circular", "dependency", "missing step"]
            ):
                classification = FailureClassification.PLAN
            else:
                classification = (
                    FailureClassification.PROVIDER
                    if "api" in stderr_lower
                    else FailureClassification.UNKNOWN
                )

        # Create evidence reference
        evidence_content = f"exit_code={exit_code}\nstdout={stdout}\nstderr={stderr}"
        evidence_hash = hashlib.sha256(evidence_content.encode()).hexdigest()[:16]
        evidence_ref = f"evidence:{evidence_hash}"
        context.redacted_evidence[evidence_ref] = (
            f"[REDACTED] exit_code={exit_code} duration={duration_ms}ms"
        )

        evidence = VerificationEvidence(
            check_name=gate.name,
            command=gate.command,
            exit_code=exit_code,
            stdout_ref=evidence_ref,
            stderr_ref=evidence_ref,
            duration_ms=duration_ms,
        )

        result = VerificationResult(
            check_name=gate.name,
            outcome=outcome,
            classification=classification,
            message=stderr[:500] if outcome != VerificationOutcome.PASS else "Passed",
            evidence=evidence,
            requires_approval=gate.requires_approval_on_fail
            and outcome == VerificationOutcome.FAIL,
            approval_reason=gate.approval_reason
            if gate.requires_approval_on_fail and outcome == VerificationOutcome.FAIL
            else "",
        )
        results.append(result)

    return all_passed, results


def can_replan(context: VerificationGateContext, reason: ReplanReason) -> tuple[bool, str]:
    """
    Check if a replan is allowed.

    Returns:
        (allowed, reason_if_denied)
    """
    # Check max replans
    if len(context.replan_records) >= context.max_replans:
        return False, f"Max replans ({context.max_replans}) exceeded"

    # Check if we have a recent failure that warrants replan
    # (This would be checked by the caller)

    return True, ""


def propose_replan(
    context: VerificationGateContext,
    reason: ReplanReason,
    new_plan: dict[str, Any],
    budget_usd: float | None = None,
    max_concurrency: int | None = None,
    required_permissions: set[str] | None = None,
    risk_floor: float | None = None,
) -> ReplanRecord:
    """
    Propose a replan with bounds checking.

    Validates that the new plan doesn't exceed bounds:
    - Budget cannot increase
    - Concurrency cannot increase
    - Permissions cannot expand
    - Risk floor cannot decrease
    """
    new_plan_hash = hashlib.sha256(json.dumps(new_plan, sort_keys=True).encode()).hexdigest()[:16]

    record = ReplanRecord(
        attempt=len(context.replan_records) + 1,
        reason=reason,
        original_plan_hash=context.current_plan_hash,
        new_plan_hash=new_plan_hash,
    )

    # Validate bounds
    if budget_usd is not None and budget_usd > context.budget_usd:
        record.budget_delta_usd = budget_usd - context.budget_usd
        raise ValueError(
            f"Replan would increase budget from ${context.budget_usd:.2f} to ${budget_usd:.2f}"
        )

    if max_concurrency is not None and max_concurrency > context.max_concurrency:
        record.concurrency_delta = max_concurrency - context.max_concurrency
        raise ValueError(
            f"Replan would increase concurrency from {context.max_concurrency} to {max_concurrency}"
        )

    if required_permissions is not None:
        new_perms = required_permissions - context.required_permissions
        if new_perms:
            record.permission_delta = list(new_perms)
            raise ValueError(f"Replan would add new permissions: {new_perms}")

    if risk_floor is not None and risk_floor < context.risk_floor:
        record.risk_floor_delta = risk_floor - context.risk_floor
        raise ValueError(
            f"Replan would decrease risk floor from {context.risk_floor} to {risk_floor}"
        )

    return record


def approve_replan(record: ReplanRecord, approver: str) -> None:
    """Approve a replan record."""
    record.approved = True
    record.approver = approver


def completion_evidence(
    task_id: str,
    attempt_id: str,
    verification_results: list[VerificationResult],
    replan_records: list[ReplanRecord],
    plan_hash: str,
) -> dict[str, Any]:
    """Generate completion evidence bundle."""
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "plan_hash": plan_hash,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "verification_results": [
            {
                "check": r.check_name,
                "outcome": r.outcome.value,
                "classification": r.classification.value if r.classification else None,
                "message": r.message,
                "requires_approval": r.requires_approval,
                "approval_reason": r.approval_reason,
            }
            for r in verification_results
        ],
        "replan_history": [
            {
                "replan_id": r.replan_id,
                "attempt": r.attempt,
                "reason": r.reason.value,
                "original_plan_hash": r.original_plan_hash,
                "new_plan_hash": r.new_plan_hash,
                "budget_delta_usd": r.budget_delta_usd,
                "concurrency_delta": r.concurrency_delta,
                "permission_delta": r.permission_delta,
                "risk_floor_delta": r.risk_floor_delta,
                "approved": r.approved,
                "approver": r.approver,
                "timestamp": r.timestamp,
            }
            for r in replan_records
        ],
        "verification_gates_passed": all(
            r.outcome == VerificationOutcome.PASS for r in verification_results
        ),
        "replans_used": len(replan_records),
    }


__all__ = [
    "FailureClassification",
    "ReplanReason",
    "ReplanRecord",
    "VerificationEvidence",
    "VerificationGate",
    "VerificationGateContext",
    "VerificationOutcome",
    "VerificationResult",
    "approve_replan",
    "can_replan",
    "completion_evidence",
    "propose_replan",
    "run_verification_gates",
]
