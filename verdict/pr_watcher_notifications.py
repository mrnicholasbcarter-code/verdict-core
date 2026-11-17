"""Transition-only notifications for the PR watcher.

A digest event is built for every new failure, blocked transition and
regression, whether or not a Linear client is configured.  A Linear comment is
queued in addition only when the PR names a linked story.  Both are
deduplicated by durable idempotency key, so a repeated observation, a duplicate
timer invocation or an overlapping run produces nothing.
"""

from __future__ import annotations

from typing import Any, Protocol

from verdict.pr_watcher_state import MAX_LINEAR_RETRY


class LinearClient(Protocol):
    """Minimal Linear client: only add_comment is exposed."""

    def add_comment(self, issue_id: str, body: str) -> dict[str, Any]: ...


# --- Transition detector ----------------------------------------------------------

# READY and NO_ACTION are absent on purpose: a healthy or still-running PR is
# never a notification.
_EVENT_TYPE_FOR_ACTION = {"RETRY": "new_failure", "REPAIR": "new_failure", "BLOCKED": "blocked"}


def transition_event_type(action: str) -> str | None:
    """Digest event type for a watcher action, or None when it is not a failure."""
    return _EVENT_TYPE_FOR_ACTION.get(action)


# --- Event builders ---------------------------------------------------------------


def build_digest_event(
    event_type: str,
    repo: str,
    pr_number: int,
    head: str,
    classification: str,
    reason: str | None,
    timestamp: str,
) -> dict[str, Any]:
    """Build a digest event whose key is stable for one transition on one head."""
    return {
        "type": event_type,
        "repo": repo,
        "pr_number": pr_number,
        "head": head,
        "classification": classification,
        "reason": reason,
        "idempotency_key": f"digest:{event_type}:{repo}:pr{pr_number}:{head}:{classification}",
        "timestamp": timestamp,
    }


def build_linear_event(issue_id: str, digest: dict[str, Any]) -> dict[str, Any]:
    """Build the Linear comment event for the story linked to a digest event."""
    return {
        **digest,
        "linear_issue_id": issue_id,
        "idempotency_key": "linear:" + digest["idempotency_key"].removeprefix("digest:"),
        "retry_count": 0,
    }


# --- Linear delivery --------------------------------------------------------------


def deliver_linear_events(
    pending: list[dict[str, Any]], linear: LinearClient, max_attempts: int = MAX_LINEAR_RETRY
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attempt each queued Linear comment once; return (remaining, sent).

    A failed event stays queued with its attempt count raised.  Once the count
    reaches ``max_attempts`` the event is no longer attempted, but it is kept:
    an undelivered notification is never dropped.
    """
    remaining: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []
    for event in pending:
        attempts = int(event.get("retry_count", 0))
        if attempts >= max_attempts:
            remaining.append(event)
            continue
        try:
            linear.add_comment(event["linear_issue_id"], _format_linear_body(event))
        except Exception:
            remaining.append({**event, "retry_count": attempts + 1})
        else:
            sent.append(event)
    return remaining, sent


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
    methods = {m for m in dir(linear) if not m.startswith("_") and callable(getattr(linear, m))}
    helpers = {m for m in methods if m.startswith(("set_", "fail_", "before_"))}
    mutations = {
        m
        for m in methods
        if m.startswith(("create_", "update_", "delete_", "merge", "push", "close", "add_label"))
    }
    bad = mutations - helpers
    if bad:
        raise TypeError(f"Watcher Linear client exposes mutation methods: {bad}")
