"""Transition-only notifications for the PR watcher.

Emits digest events and Linear comments only on state transitions
(new failure, blocked, regression).  Duplicate timer invocations,
concurrent runs, and unchanged observations produce no notification.
"""

from __future__ import annotations

from typing import Any, Protocol


class LinearClient(Protocol):
    """Minimal Linear client: only add_comment is exposed."""

    def add_comment(self, issue_id: str, body: str) -> dict[str, Any]: ...


# --- Digest event builder ---------------------------------------------------------


def build_digest_event(
    event_type: str,
    repo: str,
    pr_number: int,
    head: str,
    classification: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic digest event."""
    import time

    return {
        "type": event_type,
        "repo": repo,
        "pr_number": pr_number,
        "head": head,
        "classification": classification,
        "reason": reason,
        "idempotency_key": f"digest:{event_type}:{repo}:pr{pr_number}:{head}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --- Linear notification builder --------------------------------------------------


def build_linear_event(
    issue_id: str,
    event_type: str,
    repo: str,
    pr_number: int,
    head: str,
    classification: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a Linear comment payload with idempotency key."""
    import time

    return {
        "type": event_type,
        "linear_issue_id": issue_id,
        "repo": repo,
        "pr_number": pr_number,
        "head": head,
        "classification": classification,
        "reason": reason,
        "idempotency_key": f"linear:{event_type}:{repo}:pr{pr_number}:{head}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --- Transition detector ----------------------------------------------------------


def should_notify(previous_classification: str | None, new_classification: str) -> bool:
    """Return True only for state transitions that warrant notification.

    Notifies on:
    - Any new failure (CODE_FAILURE, INFRA_FAILURE, CANCELLED, CONFLICT, EMPTY, MISSING_REQUIRED)
    - BLOCKED transitions
    - Regression (merged with fail review)
    Does NOT notify on:
    - GREEN/PROCEED_MERGE
    - PENDING/WAIT_CI
    - Repeated identical classifications
    """
    if previous_classification == new_classification:
        return False
    if new_classification in {"GREEN", "PENDING"}:
        return False
    return new_classification in {
        "CODE_FAILURE",
        "INFRA_FAILURE",
        "CANCELLED",
        "CONFLICT",
        "EMPTY",
        "MISSING_REQUIRED",
        "BLOCKED",
        "REGRESSION",
    }


# --- Failed Linear notification retry ---------------------------------------------


class FailedNotification:
    """Tracks a failed Linear notification for bounded retry."""

    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event
        self.attempts = 0

    def increment(self) -> int:
        self.attempts += 1
        return self.attempts

    def is_exhausted(self, max_attempts: int = 5) -> bool:
        return self.attempts >= max_attempts


def retry_failed_notifications(
    pending: list[dict[str, Any]], linear: LinearClient, max_attempts: int = 5
) -> tuple[list[dict[str, Any]], list[FailedNotification]]:
    """Retry failed Linear notifications within a bounded window.

    Returns (still_pending, exhausted) where exhausted are removed from
    the pending queue but kept for diagnostic purposes.
    """
    still_pending = []
    exhausted = []
    for event in pending:
        attempt = event.get("retry_count", 0) + 1
        try:
            linear.add_comment(event["linear_issue_id"], _format_linear_body(event))
            # Success: drop from pending
        except Exception:
            if attempt >= max_attempts:
                exhausted.append(FailedNotification(event))
            else:
                event["retry_count"] = attempt
                still_pending.append(event)
    return still_pending, exhausted


def _format_linear_body(event: dict[str, Any]) -> str:
    """Format a Linear comment body from an event."""
    lines = [
        f"**PR Watcher: {event['type'].upper()}**",
        f"Repo: {event['repo']}",
        f"PR: #{event['pr_number']}",
        f"Head: {event['head'][:12]}",
        f"Classification: {event['classification']}",
    ]
    if event.get("reason"):
        lines.append(f"Reason: {event['reason']}")
    lines.append(f"Timestamp: {event['timestamp']}")
    return "\n".join(lines)


# --- Verification -----------------------------------------------------------------


def assert_no_mutation(linear: LinearClient) -> None:
    """Raise TypeError if the Linear client exposes forbidden methods."""
    forbidden = {"create_issue", "update_issue", "delete_issue", "create_branch", "push", "merge"}
    methods = {m for m in dir(linear) if not m.startswith("_")}
    bad = methods & forbidden
    if bad:
        raise TypeError(f"Linear client must not expose mutation methods: {bad}")
