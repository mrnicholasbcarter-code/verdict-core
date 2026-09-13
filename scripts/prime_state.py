#!/usr/bin/env python3
"""Durable state, lease/fencing, checkpoint and CI contracts for Prime workflow."""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


STATES = (
    "READY",
    "HYDRATED",
    "IMPLEMENTING",
    "LOCAL_VALIDATION",
    "PROOF_COMPLETE",
    "REVIEW_APPROVED",
    "PR_OPEN",
    "CI_GREEN",
    "MERGED",
    "MAIN_VERIFIED",
    "DONE",
)
GATES = {"STATIC", "UNIT", "INTEGRATION", "ACCEPTANCE-PROOF"}


# --- Durable issue leases and fencing -------------------------------------

LEASE_FIELDS = (
    "schema_version",
    "issue",
    "dispatch_id",
    "worker_id",
    "worktree",
    "branch",
    "generation",
    "status",
    "acquired_at",
    "heartbeat_at",
    "last_progress_at",
    "stale_after_seconds",
    "supervisor_id",
    "superseded_reason",
)


def _iso(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()


def lease_dir(state: Path, issue: str) -> Path:
    require(bool(issue) and "/" not in issue and ".." not in issue, "invalid issue identifier")
    return state / "leases" / issue


def read_lease(state: Path, issue: str) -> dict[str, Any] | None:
    path = lease_dir(state, issue) / "active.json"
    if not path.is_file():
        return None
    lease: dict[str, Any] = json.loads(path.read_text())
    return lease


def read_lease_history(state: Path, issue: str) -> list[dict[str, Any]]:
    directory = lease_dir(state, issue)
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("generation-*.json")):
        records.append(json.loads(path.read_text()))
    records.sort(key=lambda record: record["generation"])
    return records


def lease_is_stale(lease: dict[str, Any], now: float) -> bool:
    stale_after = lease.get("stale_after_seconds")
    last_progress = lease.get("last_progress_at")
    if (
        not isinstance(stale_after, (int, float))
        or isinstance(stale_after, bool)
        or stale_after <= 0
    ):
        raise ValueError("lease needs a positive numeric staleness bound")
    if not isinstance(last_progress, (int, float)) or isinstance(last_progress, bool):
        raise ValueError("lease needs a numeric last_progress_at")
    return (now - float(last_progress)) > float(stale_after)


def _persist_lease(state: Path, lease: dict[str, Any]) -> dict[str, Any]:
    directory = lease_dir(state, lease["issue"])
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_json(directory / f"generation-{lease['generation']}.json", lease)
    atomic_write_json(directory / "active.json", lease)
    # Durable marker; the common supervisor flock provides process exclusion.
    (directory / "lease.lock").write_text(f"generation={lease['generation']}\n")
    return lease


def acquire_lease(
    state: Path,
    *,
    issue: str,
    dispatch_id: str,
    worker_id: str,
    worktree: str,
    branch: str,
    stale_after_seconds: float,
    supervisor_id: str,
    now: float,
) -> dict[str, Any]:
    """Single-writer lease. A live owner blocks; a stale owner is fenced and superseded."""
    require(bool(dispatch_id) and bool(worker_id), "lease needs dispatch and worker identity")
    require(stale_after_seconds > 0, "lease staleness bound must be positive")
    existing = read_lease(state, issue)
    generation = 1
    if existing is not None:
        generation = int(existing["generation"]) + 1
        if existing["status"] == "ACTIVE" and not lease_is_stale(existing, now):
            raise ValueError(
                f"lease held by worker {existing['worker_id']} "
                f"(dispatch {existing['dispatch_id']}, generation {existing['generation']}); "
                "stop and supersede it before assigning a new writer"
            )
        if existing["status"] == "ACTIVE":
            fenced = {**existing, "status": "SUPERSEDED", "superseded_reason": "stale_progress"}
            _persist_lease(state, fenced)
    lease = {
        "schema_version": 1,
        "issue": issue,
        "dispatch_id": dispatch_id,
        "worker_id": worker_id,
        "worktree": worktree,
        "branch": branch,
        "generation": generation,
        "status": "ACTIVE",
        "acquired_at": now,
        "acquired_at_iso": _iso(now),
        "heartbeat_at": now,
        "last_progress_at": now,
        "stale_after_seconds": stale_after_seconds,
        "supervisor_id": supervisor_id,
        "superseded_reason": None,
    }
    return _persist_lease(state, lease)


def heartbeat_lease(
    state: Path, issue: str, *, generation: int, progress: bool, now: float
) -> dict[str, Any]:
    """Liveness is not progress: only `progress=True` moves the stall deadline."""
    active = read_lease(state, issue)
    require(active is not None, "no lease to heartbeat")
    require(
        active is not None
        and int(active["generation"]) == int(generation)
        and active["status"] == "ACTIVE",
        f"writer is fenced (active generation {active['generation'] if active else None}); "
        "its writes are superseded",
    )
    assert active is not None
    updated = {**active, "heartbeat_at": now, "heartbeat_at_iso": _iso(now)}
    if progress:
        updated["last_progress_at"] = now
        updated["last_progress_at_iso"] = _iso(now)
    return _persist_lease(state, updated)


def release_lease(state: Path, issue: str, *, generation: int, now: float) -> dict[str, Any]:
    active = read_lease(state, issue)
    require(active is not None, "no lease to release")
    require(
        active is not None and int(active["generation"]) == int(generation),
        "only the current lease generation may release",
    )
    assert active is not None
    return _persist_lease(state, {**active, "status": "RELEASED", "released_at": now})


def supersede_stale_leases(state: Path, *, now: float) -> list[str]:
    """Recover stale writers without an operator hunting for processes."""
    reaped: list[str] = []
    root = state / "leases"
    if not root.is_dir():
        return reaped
    for directory in sorted(root.iterdir()):
        active_path = directory / "active.json"
        if not active_path.is_file():
            continue
        lease = json.loads(active_path.read_text())
        if lease.get("status") == "ACTIVE" and lease_is_stale(lease, now):
            _persist_lease(
                state, {**lease, "status": "SUPERSEDED", "superseded_reason": "stale_progress"}
            )
            reaped.append(str(lease["issue"]))
    return reaped


def require_lease_for_pr(
    state: Path,
    packet: dict[str, Any],
    *,
    now: float | None = None,
    lease_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fencing gate: only the current, unstale lease holder may open or update a PR."""
    observed = now if now is not None else time.time()
    lease = lease_hint if lease_hint is not None else read_lease(state, str(packet["issue"]))
    require(lease is not None, "no active issue lease; dispatch must acquire one first")
    assert lease is not None
    require(
        lease.get("status") == "ACTIVE",
        f"lease is not active ({lease.get('status')}); writer is fenced",
    )
    require(
        lease.get("dispatch_id") == packet.get("attempt_id"),
        "lease dispatch_id does not match the hydrated packet attempt",
    )
    active = read_lease(state, str(packet["issue"]))
    require(
        active is not None and lease == active,
        "lease generation or identity superseded; this writer is fenced",
    )
    require(not lease_is_stale(lease, observed), "lease is stale; rehydrate and redispatch first")
    return lease


# --- Durable checkpoint ----------------------------------------------------

CHECKPOINT_FIELDS = (
    "schema_version",
    "issue",
    "project",
    "team",
    "state",
    "worktree",
    "branch",
    "base_sha",
    "head_sha",
    "attempt_id",
    "provider",
    "model",
    "packet_path",
    "receipt_path",
    "proof_path",
    "pr_url",
    "merge_sha",
    "main_sha",
    "last_progress_at",
    "heartbeat_at",
    "next_action",
    "blockers",
    "completed_issues",
    "updated_at",
)


def atomic_write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temp.replace(path)
    return path


def checkpoint_skeleton(issue: str, state: str, **overrides: Any) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {key: None for key in CHECKPOINT_FIELDS}
    checkpoint.update(
        {
            "schema_version": 1,
            "issue": issue,
            "state": state,
            "blockers": [],
            "completed_issues": [],
            "next_action": f"resume {issue} at {state}",
            "last_progress_at": _iso(time.time()),
            "heartbeat_at": _iso(time.time()),
            "updated_at": _iso(time.time()),
        }
    )
    checkpoint.update(overrides)
    return checkpoint


def validate_checkpoint(checkpoint: dict[str, Any]) -> None:
    for key in CHECKPOINT_FIELDS:
        require(key in checkpoint, f"checkpoint missing {key}")
    require(checkpoint.get("schema_version") == 1, "checkpoint schema_version must be 1")
    require(checkpoint.get("issue"), "checkpoint requires an issue")
    require(
        checkpoint.get("state") in STATES, f"unknown checkpoint state: {checkpoint.get('state')}"
    )
    require(bool(checkpoint.get("next_action")), "checkpoint requires an explicit next_action")
    require(isinstance(checkpoint.get("blockers"), list), "blockers must be a list")
    require(isinstance(checkpoint.get("completed_issues"), list), "completed_issues must be a list")
    if checkpoint.get("verified_state") is not None:
        require(checkpoint["verified_state"] in STATES, "verified_state must be a lifecycle state")
        require(bool(checkpoint.get("proof_digest")), "verified_state requires a proof_digest")


def read_checkpoint(state: Path) -> dict[str, Any]:
    path = state / "checkpoint.json"
    require(path.is_file(), "no durable checkpoint; nothing to resume")
    checkpoint: dict[str, Any] = json.loads(path.read_text())
    validate_checkpoint(checkpoint)
    return checkpoint


def write_checkpoint(state: Path, checkpoint: dict[str, Any]) -> Path:
    validate_checkpoint(checkpoint)
    path = state / "checkpoint.json"
    if path.is_file():
        try:
            previous = json.loads(path.read_text())
        except (ValueError, OSError):
            previous = {}
        if (
            previous.get("issue") == checkpoint["issue"]
            and previous.get("state") in STATES
            and STATES.index(str(previous["state"])) > STATES.index(str(checkpoint["state"]))
        ):
            raise ValueError(
                f"refusing to regress {checkpoint['issue']} from {previous['state']} "
                f"to {checkpoint['state']}; resume forward from the verified state"
            )
        if previous.get("issue") != checkpoint["issue"]:
            completed = list(previous.get("completed_issues") or [])
            if previous.get("state") == "DONE" and previous["issue"] not in completed:
                completed.append(previous["issue"])
            checkpoint = {**checkpoint, "completed_issues": completed}
    checkpoint = {**checkpoint, "updated_at": _iso(time.time())}
    return atomic_write_json(path, checkpoint)


def next_action_from_checkpoint(state: Path) -> dict[str, Any]:
    """Cold-resume projection: what a fresh process must do next, from disk only."""
    checkpoint = read_checkpoint(state)
    return {
        "issue": checkpoint["issue"],
        "state": checkpoint["state"],
        "next_action": checkpoint["next_action"],
        "worktree": checkpoint["worktree"],
        "branch": checkpoint["branch"],
        "head_sha": checkpoint["head_sha"],
        "attempt_id": checkpoint["attempt_id"],
        "blockers": checkpoint["blockers"],
        "completed_issues": checkpoint["completed_issues"],
    }


# --- CI / merge classification and bounded recovery ------------------------

INFRA_PATTERNS = (
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


# --- Meaningful progress ---------------------------------------------------


def is_progress(
    previous_substantive: str,
    previous_artifact: str,
    previous_receipt: str,
    substantive: str,
    artifact: str,
    receipt_identity: str,
) -> bool:
    """Progress is substantive source/state change or a receipt transition.

    Artifact churn alone (a hung worker rewriting or adding logs) is not progress.
    """
    if substantive != previous_substantive:
        return True
    if receipt_identity != previous_receipt:
        return True
    _ = (previous_artifact, artifact)
    return False
