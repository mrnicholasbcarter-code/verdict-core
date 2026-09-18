"""Evidence-bound execution contract for the paired savings bench (BOD-114).

A savings claim is only possible when *both* arms of a task were actually
executed and every piece of evidence is bound to those executions:

* an execution/run ID returned by the gateway,
* the same canonical ``input_hash`` supplied to both arms,
* cost and token usage read only from observed response headers / receipts,
* a provider-bound ``completed_with`` identity, with the Verdict arm's
  routed → completed attempt chain explained,
* quality evaluated from the produced output against the task's checks.

``ArmExecutor`` is the hook a live runner implements. The offline fixture
mode never provides one, so it can never claim.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

ARM_DIRECT = "direct"
ARM_VERDICT = "verdict"
ARMS = (ARM_DIRECT, ARM_VERDICT)

EVIDENCE_HEADER_PREFIX = "x-omniroute-"
EXECUTION_ID_HEADERS = ("x-omniroute-request-id", "x-request-id", "x-verdict-request-id")


def canonical_input_hash(prompt: str, acceptance_criteria: Sequence[str]) -> str:
    """Hash of the exact task input both arms must receive."""
    payload = {"prompt": prompt, "acceptance_criteria": list(acceptance_criteria)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def output_digest(output: str) -> str:
    return f"sha256:{hashlib.sha256(output.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class ArmRequest:
    """What an executor is asked to run. Identical input for both arms."""

    arm: str
    task_id: str
    prompt: str
    acceptance_criteria: tuple[str, ...]
    input_hash: str
    model: str
    # Verdict arm only: the compiled, hydrated context pack (BOD-111 envelope).
    context_envelope: str | None = None
    # Verdict arm only: routing evidence the execution must be bound to.
    routed_model: str | None = None
    attempt_chain: tuple[str, ...] = ()
    pack_digest: str | None = None
    prompt_digest: str | None = None

    def messages(self) -> list[dict[str, str]]:
        """OpenAI-style messages: optional system envelope, then the identical task."""
        items: list[dict[str, str]] = []
        if self.context_envelope:
            items.append({"role": "system", "content": self.context_envelope})
        criteria = "\n".join(f"- {item}" for item in self.acceptance_criteria)
        items.append(
            {"role": "user", "content": f"{self.prompt}\n\nAcceptance criteria:\n{criteria}"}
        )
        return items


@dataclass(frozen=True)
class ArmExecution:
    """Observed evidence returned by an executor for one arm."""

    arm: str
    execution_id: str
    input_hash: str
    completed_with: str
    gateway: str
    output: str
    headers: Mapping[str, str] = field(default_factory=dict)
    receipt: Mapping[str, Any] = field(default_factory=dict)
    attempt_chain: tuple[str, ...] = ()
    status_code: int | None = None

    def evidence_headers(self) -> dict[str, str]:
        """Only gateway evidence headers are retained (privacy review: no auth, no body)."""
        return {
            key.lower(): value
            for key, value in self.headers.items()
            if key.lower().startswith(EVIDENCE_HEADER_PREFIX) or key.lower() in EXECUTION_ID_HEADERS
        }

    def to_evidence(self) -> dict[str, Any]:
        """Privacy-reviewed bundle row: digests, IDs, identities, headers — never output text."""
        return {
            "arm": self.arm,
            "execution_id": self.execution_id,
            "input_hash": self.input_hash,
            "completed_with": self.completed_with,
            "gateway": self.gateway,
            "attempt_chain": list(self.attempt_chain),
            "status_code": self.status_code,
            "output_digest": output_digest(self.output),
            "headers": self.evidence_headers(),
            "receipt": {k: v for k, v in self.receipt.items() if k != "body"},
        }


ArmExecutor = Callable[[ArmRequest], ArmExecution]


@dataclass(frozen=True)
class QualityResult:
    passed: bool
    misses: tuple[str, ...]
    evaluator: str

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "misses": list(self.misses), "evaluator": self.evaluator}


QualityEvaluator = Callable[[Mapping[str, Any], ArmExecution], QualityResult]


def validate_quality(passed: object, misses: object, *, where: str) -> tuple[bool, tuple[str, ...]]:
    """Reject contradictory quality data: passed=true with named misses is not a pass."""
    if not isinstance(passed, bool):
        raise ValueError(f"{where}: quality.passed must be a boolean")
    if not isinstance(misses, list) or any(not isinstance(item, str) for item in misses):
        raise ValueError(f"{where}: quality.misses must be a list of strings")
    if passed and misses:
        raise ValueError(
            f"{where}: contradictory quality — passed=true with misses {misses!r} is rejected"
        )
    return passed, tuple(misses)


def evaluate_output_against_checks(
    task: Mapping[str, Any], execution: ArmExecution
) -> QualityResult:
    """Default evaluator: the task's ``checks.must_contain`` phrases must appear in the output.

    When a task declares no checks, quality is *not* passed — a claim needs
    evidence, and "no checks" is not evidence.
    """
    checks = task.get("checks")
    must_contain = checks.get("must_contain") if isinstance(checks, Mapping) else None
    if not isinstance(must_contain, list) or not must_contain:
        return QualityResult(
            passed=False, misses=("no acceptance checks declared for this task",), evaluator="none"
        )
    lowered = execution.output.lower()
    misses = tuple(
        f"output missing required phrase: {phrase!r}"
        for phrase in must_contain
        if not isinstance(phrase, str) or phrase.lower() not in lowered
    )
    return QualityResult(passed=not misses, misses=misses, evaluator="checks.must_contain")


def execution_evidence_gaps(
    request: ArmRequest, execution: ArmExecution | None, *, cost_error: str | None
) -> list[str]:
    """Name every reason this arm's evidence cannot back a claim."""
    gaps: list[str] = []
    if execution is None:
        return [f"{request.arm}:not_executed"]
    if not execution.execution_id.strip():
        gaps.append(f"{request.arm}:missing_execution_id")
    if execution.input_hash != request.input_hash:
        gaps.append(f"{request.arm}:input_hash_mismatch")
    if not execution.completed_with.strip():
        gaps.append(f"{request.arm}:identity_unbound")
    if cost_error is not None:
        gaps.append(f"{request.arm}:cost_not_observed")
    if execution.status_code is not None and not 200 <= execution.status_code < 300:
        gaps.append(f"{request.arm}:upstream_status_{execution.status_code}")
    if request.arm == ARM_VERDICT:
        routed = request.routed_model or ""
        chain = tuple(execution.attempt_chain or request.attempt_chain)
        if routed and execution.completed_with != routed and execution.completed_with not in chain:
            # A completed identity that is neither the routed pick nor a named
            # fallback in the attempt chain cannot be explained; refuse.
            gaps.append(f"{request.arm}:completed_identity_not_in_attempt_chain")
    return gaps


def explain_identity(request: ArmRequest, execution: ArmExecution) -> dict[str, Any]:
    """Bind routed → completed identity for the Verdict arm and explain any difference."""
    routed = request.routed_model
    completed = execution.completed_with
    chain = list(execution.attempt_chain or request.attempt_chain)
    if routed is None:
        explanation = "direct arm: fixed frontier identity"
    elif completed == routed:
        explanation = "completed with the routed identity"
    elif completed in chain:
        explanation = f"routed {routed} fell back within the attempt chain to {completed}"
    else:
        explanation = f"completed identity {completed} is not in the attempt chain — unbound"
    return {
        "routed": routed,
        "completed_with": completed,
        "gateway": execution.gateway,
        "attempt_chain": chain,
        "bound": routed is None or completed == routed or completed in chain,
        "explanation": explanation,
    }


__all__ = [
    "ARMS",
    "ARM_DIRECT",
    "ARM_VERDICT",
    "ArmExecution",
    "ArmExecutor",
    "ArmRequest",
    "QualityEvaluator",
    "QualityResult",
    "canonical_input_hash",
    "evaluate_output_against_checks",
    "execution_evidence_gaps",
    "explain_identity",
    "output_digest",
    "validate_quality",
]
