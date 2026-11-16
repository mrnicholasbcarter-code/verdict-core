"""CI classification and bounded recovery policy.

Deterministic classifiers and recovery actions for GitHub check results and
merge state.  This module is the importable production authority;
`scripts/prime_state.py` re-exports these names for backward compatibility.
"""

from __future__ import annotations

from typing import Any

INFRA_PATTERNS: tuple[str, ...] = (
    "runner",
    "availability",
    "infrastructure",
    "network",
    "quota",
    "rate-limit",
    "timeout",
    "dependency-download",
    "artifact-upload",
)


def classify_ci(checks: list[dict[str, Any]]) -> str:
    """Deterministic classification of GitHub check results for the exact PR head."""
    if not checks:
        return "EMPTY"
    required = [c for c in checks if c.get("required") is True]
    if not required:
        return "MISSING_REQUIRED"
    for check in required:
        if check.get("status") != "COMPLETED" or check.get("conclusion") is None:
            return "PENDING"
    failures = [c for c in required if c.get("conclusion") == "FAILURE"]
    if failures:
        for check in failures:
            name = str(check.get("name", "")).lower()
            if not any(pattern in name for pattern in INFRA_PATTERNS):
                return "CODE_FAILURE"
        return "INFRA_FAILURE"
    if any(c.get("conclusion") == "CANCELLED" for c in required):
        return "CANCELLED"
    if any(c.get("conclusion") not in {"SUCCESS", "SKIPPED", "NEUTRAL"} for c in required):
        return "PENDING"
    return "GREEN"


def classify_merge_state(mergeable: str, merge_state_status: str) -> str:
    """Classify mergeability from GitHub mergeability and merge_state_status fields."""
    if mergeable == "CONFLICTING" or merge_state_status == "DIRTY":
        return "CONFLICT"
    if merge_state_status == "BEHIND":
        return "BEHIND"
    if mergeable == "MERGEABLE" and merge_state_status == "CLEAN":
        return "MERGEABLE"
    return "UNKNOWN"


def ci_recovery_action(classification: str, attempts: int, max_attempts: int) -> dict[str, Any]:
    """CI failure routes back into corrective work; it is never reported as success."""
    if classification == "GREEN":
        return {
            "action": "PROCEED_MERGE",
            "state": "CI_GREEN",
            "reason": "all required checks passed",
        }
    if classification == "PENDING":
        return {"action": "WAIT_CI", "state": "PR_OPEN", "reason": "required checks not concluded"}
    if classification in {"INFRA_FAILURE", "CANCELLED"}:
        if attempts >= max_attempts:
            return {
                "action": "BLOCKED",
                "state": "PR_OPEN",
                "reason": "infrastructure retries exhausted",
            }
        return {
            "action": "RETRY_CI",
            "state": "PR_OPEN",
            "reason": "transient infrastructure failure",
        }
    if classification == "CONFLICT":
        return {
            "action": "REBASE_AND_REPROOF",
            "state": "IMPLEMENTING",
            "reason": "base drift or merge conflict",
        }
    if classification == "CODE_FAILURE":
        if attempts >= max_attempts:
            return {
                "action": "BLOCKED",
                "state": "PR_OPEN",
                "reason": "corrective attempts exhausted",
            }
        return {
            "action": "CORRECTIVE_IMPLEMENTATION",
            "state": "IMPLEMENTING",
            "reason": "required check failed on the PR head",
        }
    return {
        "action": "BLOCKED",
        "state": "PR_OPEN",
        "reason": f"unclassifiable CI state: {classification}",
    }
