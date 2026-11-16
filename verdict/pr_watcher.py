"""PR watcher core: exact-head polling, action adaptation, and reconciliation.

Pure orchestration: loads GitHub facts, classifies PRs, applies bounded
recovery, writes durable state before side effects, and emits transition
notifications only.  No LLM or model-routing calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.ci_policy import classify_ci, classify_merge_state
from verdict.pr_watcher_github import WatcherGitHubClient
from verdict.pr_watcher_github import assert_no_mutation as assert_github_no_mutation
from verdict.pr_watcher_notifications import (
    LinearClient,
    build_digest_event,
    build_linear_event,
    retry_failed_notifications,
    should_notify,
)
from verdict.pr_watcher_notifications import assert_no_mutation as assert_linear_no_mutation
from verdict.pr_watcher_state import (
    BlockRecord,
    MalformedStateError,
    RegressionRecord,
    RepairRecord,
    WatcherState,
    format_timestamp,
)

# --- Action adapter (from spec table) --------------------------------------------

# Adapter actions: maps classifier outputs to watcher behaviour
_READY = frozenset({"GREEN", "PROCEED_MERGE"})
_NO_ACTION = frozenset({"PENDING", "WAIT_CI"})
_RETRY_ELIGIBLE = frozenset({"INFRA_FAILURE", "CANCELLED", "STALLED", "RETRY_CI"})
_REPAIR_NEEDED = frozenset(
    {"CODE_FAILURE", "CORRECTIVE_IMPLEMENTATION", "CONFLICT", "BEHIND", "REBASE_AND_REPROOF"}
)
_NOT_GREEN = frozenset({"EMPTY", "MISSING_REQUIRED"})
_BLOCKED = frozenset({"BLOCKED"})


def _adapter_action(classification: str, attempt: int, max_attempts: int) -> str:
    """Determine watcher action from CI classification.

    This implements the adapter table from the PR watcher spec verbatim.
    """
    if classification in _READY:
        return "READY"
    if classification in _NO_ACTION:
        return "NO_ACTION"
    if classification in _RETRY_ELIGIBLE:
        if attempt >= max_attempts:
            return "BLOCKED"
        return "RETRY"
    if classification in _REPAIR_NEEDED:
        return "REPAIR"
    if classification in _NOT_GREEN:
        return "REPAIR"
    return "NO_ACTION"


# --- Core polling ---------------------------------------------------------------


def poll_open_prs(
    gh: WatcherGitHubClient, state: WatcherState, repo: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Classify every open PR at its exact head SHA.

    Returns a list of observations with classification, action, and state.
    """
    observations: list[dict[str, Any]] = []
    prs = gh.list_open_prs(repo)

    for pr in prs:
        head = pr["head_sha"]
        pr_num = pr["number"]

        # Fetch checks at exact head
        checks = gh.get_checks(repo, head)

        # Add required flag based on check conclusion presence
        required_checks = []
        for c in checks:
            c_with_required = {**c, "required": True}
            required_checks.append(c_with_required)

        # Stalled detection: >30 minutes in progress
        stalled = _detect_stalled(required_checks, head, now)

        # CI classification
        ci_class = classify_ci(required_checks)
        if stalled:
            ci_class = "STALLED"  # override to stalled when checks hang

        # Merge state
        merge_class = classify_merge_state(
            str(pr.get("mergeable", "")), str(pr.get("merge_state_status", ""))
        )

        # Combined classification
        combined = _combine_classifications(ci_class, merge_class)

        action = _adapter_action(combined, 0, 1)
        obs = {
            "repo": repo,
            "pr_number": pr_num,
            "head": head,
            "ci_classification": ci_class,
            "merge_classification": merge_class,
            "classification": combined,
            "action": action,
            "checks": required_checks,
            "merge_state": pr.get("merge_state_status", ""),
        }
        observations.append(obs)

    return observations


def _detect_stalled(checks: list[dict[str, Any]], head: str, now: datetime | None = None) -> bool:
    """Return True if any required check has been in progress for over 30 minutes."""
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("injected clock must be timezone-aware")
    threshold = 30 * 60
    for check in checks:
        if check.get("status") not in {"IN_PROGRESS", "QUEUED"}:
            continue
        started_at = check.get("started_at")
        if started_at is None:
            continue
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if started.tzinfo is None or started.utcoffset() is None:
            continue
        if (current - started).total_seconds() > threshold:
            return True
    return False


def _combine_classifications(ci: str, merge: str) -> str:
    """Combine CI and merge classifications into a single result.

    Merge issues (CONFLICT, BEHIND) take precedence over CI status
    when the PR is not mergeable.
    """
    if merge in {"CONFLICT", "BEHIND"}:
        return merge
    return ci


# --- Merged PR reconciliation ---------------------------------------------------

# Re-list this far behind the cursor: GitHub search lags merges, and a merge
# can be stamped slightly before the run that should have seen it.
MERGED_LOOKBACK = timedelta(minutes=5)
# Window for a run with no usable cursor, so the first query is never unbounded.
MERGED_INITIAL_LOOKBACK = timedelta(hours=24)


def _resolve_clock(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("injected clock must be timezone-aware")
    return current


def _merged_since(cursor: datetime | None, now: datetime) -> str:
    """Lower bound for the merged-PR query; always a concrete instant."""
    # A cursor ahead of the clock would hide every merge until the clock
    # caught up, so it is treated like a missing one.
    if cursor is None or cursor > now:
        return format_timestamp(now - MERGED_INITIAL_LOOKBACK)
    return format_timestamp(cursor - MERGED_LOOKBACK)


def reconcile_merged_prs(
    gh: WatcherGitHubClient,
    state: WatcherState,
    repo: str,
    verdict_fail_receipts: dict[str, dict[str, Any]] | None = None,
    data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Discover closed+merged PRs since the durable cursor minus a bounded lookback.

    A merged PR with a fail review (GitHub CHANGES_REQUESTED or
    Verdict/ocr fail receipt) becomes a regression record.
    """
    current_data = state.load() if data is None else data
    since = _merged_since(state.get_merged_cursor(current_data), _resolve_clock(now))
    merged_prs = gh.list_merged_prs(repo, since)

    regressions: list[dict[str, Any]] = []
    verdict_fail = verdict_fail_receipts or {}

    for pr in merged_prs:
        pr_num = pr["number"]
        head = pr["head_sha"]
        reviews = gh.get_reviews(repo, pr_num)

        # Check for CHANGES_REQUESTED
        has_changes_requested = any(r["state"] == "CHANGES_REQUESTED" for r in reviews)

        # Check for Verdict/ocr fail receipt
        fail_key = f"{repo}:{pr_num}:{head}"
        has_verdict_fail = verdict_fail.get(fail_key, {}).get("status") == "FAIL"

        fail_evidence: list[str] = []
        if has_changes_requested:
            fail_evidence.append("github_changes_requested")
        if has_verdict_fail:
            fail_evidence.append("verdict_ocr_fail")

        # Disagreeing evidence: GitHub APPROVED but Verdict/ocr fails
        has_approved = any(r["state"] == "APPROVED" for r in reviews)
        if has_approved and has_verdict_fail:
            fail_evidence.append("disagreeing_evidence")

        if fail_evidence:
            rec = RegressionRecord(
                repo=repo,
                pr_number=pr_num,
                head=head,
                review_sources=fail_evidence,
                reason="merged PR had fail review evidence",
            )
            regressions.append({"record": rec.to_dict(), "pr": pr, "review_sources": fail_evidence})

    return regressions


# --- Bounded rerun with fencing --------------------------------------------------


def request_bounded_rerun(
    gh: WatcherGitHubClient,
    state: WatcherState,
    repo: str,
    pr_number: int,
    head: str,
    check_name: str,
    workflow_job_id: str,
    max_attempts: int = 1,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Request a bounded rerun with exact-head fencing.

    1. Read current state
    2. Re-read PR head and check (fence against changed head)
    3. Increment attempt counter atomically BEFORE rerun
    4. Persist state
    5. Only then request the rerun
    """
    if data is None:
        data = state.load()

    # Re-read PR head immediately before rerun (fencing)
    pr = gh.get_pr(repo, pr_number)
    current_head = pr["head_sha"]

    # Fence: head must match
    if current_head != head:
        return {
            "rerun": False,
            "reason": f"head changed: expected {head[:12]}, got {current_head[:12]}",
            "type": "fence_mismatch",
        }

    # Build rerun key
    rerun_key = f"{repo}:pr{pr_number}:{head}:{check_name}:{workflow_job_id}"

    # Check attempt count
    attempt = state.get_rerun_attempt(data, rerun_key)
    if attempt >= max_attempts:
        return {
            "rerun": False,
            "reason": f"max attempts ({max_attempts}) exhausted for {rerun_key}",
            "type": "exhausted",
        }

    # Increment attempt atomically BEFORE the rerun call
    updated_data = state.set_rerun_attempt(data, rerun_key, attempt + 1)
    if data is not updated_data:
        data.clear()
        data.update(updated_data)

    # Persist state before side effect
    try:
        state.save(data)
    except MalformedStateError as exc:
        return {"rerun": False, "reason": f"state save failed: {exc}", "type": "state_error"}

    # Now request the rerun
    try:
        result = gh.rerun_check(workflow_job_id, repo)
        return {"rerun": True, "result": result}
    except Exception as exc:
        return {"rerun": False, "reason": str(exc), "type": "api_error"}


# --- Run coordinator -------------------------------------------------------------


def run_watcher(
    gh: WatcherGitHubClient,
    state: WatcherState,
    repos: list[str],
    linear: LinearClient | None = None,
    max_attempts: int = 1,
    verdict_fail_receipts: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one watcher run: poll, classify, adapt, persist, notify.

    State is written atomically before any side effects.
    No LLM or model-routing calls are made.
    """
    current = _resolve_clock(now)
    # Acquire non-blocking lock
    try:
        assert_github_no_mutation(gh)
        if linear is not None:
            assert_linear_no_mutation(linear)
        state.acquire_lock()
    except Exception as exc:
        return {"error": str(exc), "type": "lock_failure"}

    try:
        # Load state - fail closed on malformed data
        try:
            data = state.load()
        except MalformedStateError as exc:
            return {"error": str(exc), "type": "malformed_state"}

        observations: list[dict[str, Any]] = []
        reruns: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        notifications: list[dict[str, Any]] = []

        # Retry durable Linear events before creating new side effects.
        if linear is not None:
            pending = state.get_pending_events(data)
            if pending:
                still_pending, _ = retry_failed_notifications(pending, linear)
                data = {**data, "pending_events": still_pending}

        for repo in repos:
            # 1. Poll open PRs
            obs = poll_open_prs(gh, state, repo, now=current)
            observations.extend(obs)

            for obs_item in obs:
                prev_state = state.get_pr_state(data, repo, obs_item["pr_number"], obs_item["head"])
                prev_class = prev_state["classification"] if prev_state else None
                new_class = obs_item["classification"]
                action = obs_item["action"]

                # Apply adapter action
                if action == "RETRY":
                    # Once the persisted budget is spent, record one stable block.
                    if any(
                        state.get_rerun_attempt(
                            data,
                            f"{repo}:pr{obs_item['pr_number']}:{obs_item['head']}:{c['name']}:{c.get('check_run_id', '')}",
                        )
                        >= max_attempts
                        for c in obs_item["checks"]
                        if c.get("conclusion") in ("FAILURE", "CANCELLED")
                        or obs_item["ci_classification"] == "STALLED"
                    ):
                        blk = BlockRecord(
                            repo,
                            obs_item["pr_number"],
                            obs_item["head"],
                            f"attempts exhausted for {new_class}",
                        )
                        record = blk.to_dict()
                        if not state.has_record(data, record["idempotency_key"]):
                            data = state.add_record(data, record)
                            records.append(record)
                        action = "BLOCKED"
                    # Find the failing check
                    for check in obs_item["checks"]:
                        if action != "BLOCKED" and (
                            check.get("conclusion") in ("FAILURE", "CANCELLED")
                            or obs_item["ci_classification"] == "STALLED"
                        ):
                            rerun = request_bounded_rerun(
                                gh,
                                state,
                                repo,
                                obs_item["pr_number"],
                                obs_item["head"],
                                check["name"],
                                check.get("check_run_id", ""),
                                max_attempts,
                                data,
                            )
                            reruns.append(rerun)
                            break

                elif action == "REPAIR":
                    rec = RepairRecord(
                        repo=repo,
                        pr_number=obs_item["pr_number"],
                        head=obs_item["head"],
                        failing_checks=[
                            c["name"]
                            for c in obs_item["checks"]
                            if c.get("conclusion") == "FAILURE"
                        ],
                        classification=new_class,
                        reason=f"PR classified as {new_class}",
                    )
                    record = rec.to_dict()
                    if not state.has_record(data, record["idempotency_key"]):
                        data = state.add_record(data, record)
                        records.append(record)

                elif action == "BLOCKED":
                    blk = BlockRecord(
                        repo=repo,
                        pr_number=obs_item["pr_number"],
                        head=obs_item["head"],
                        reason=f"attempts exhausted for {new_class}",
                    )
                    record = blk.to_dict()
                    if not state.has_record(data, record["idempotency_key"]):
                        data = state.add_record(data, record)
                        records.append(record)

                # Update PR state
                data = state.set_pr_state(
                    data, repo, obs_item["pr_number"], obs_item["head"], new_class, action=action
                )

                # Transition notifications
                if should_notify(prev_class, new_class):
                    event = build_digest_event(
                        "classification_change",
                        repo,
                        obs_item["pr_number"],
                        obs_item["head"],
                        new_class,
                        reason=f"transitioned from {prev_class or 'unknown'} to {new_class}",
                    )
                    notifications.append(event)

            # 2. Reconcile merged PRs
            regressions = reconcile_merged_prs(
                gh, state, repo, verdict_fail_receipts, data, current
            )
            for reg in regressions:
                if not state.has_record(data, reg["record"]["idempotency_key"]):
                    data = state.add_record(data, reg["record"])
                    records.append(reg["record"])

                if should_notify(None, "REGRESSION"):
                    event = build_digest_event(
                        "regression",
                        repo,
                        reg["pr"]["number"],
                        reg["pr"]["head_sha"],
                        "REGRESSION",
                        reason=f"merged with fail review: {', '.join(reg['review_sources'])}",
                    )
                    notifications.append(event)

        # Every repository reconciled, so the cursor may move; a run that
        # raised above keeps the old cursor and re-covers the same window.
        data = state.set_merged_cursor(data, current)

        # Persist state before side effects
        state.save(data)

        # Side effects: notifications
        sent: list[dict[str, Any]] = []
        if linear is not None:
            for event in notifications:
                if linear is not None:
                    linear_event = build_linear_event(
                        event["idempotency_key"],
                        event["type"],
                        event["repo"],
                        event["pr_number"],
                        event["head"],
                        event["classification"],
                        event.get("reason"),
                    )
                    try:
                        linear.add_comment(
                            event["idempotency_key"],
                            f"PR {event['repo']}#{event['pr_number']}: {event['classification']}",
                        )
                        sent.append(linear_event)
                    except Exception:
                        data = state.add_pending_event(data, {**linear_event, "retry_count": 0})
                        state.save(data)

        return {
            "observations": observations,
            "reruns": reruns,
            "records": records,
            "notifications": sent,
            "pending_events": data.get("pending_events", []),
        }

    finally:
        state.release_lock()
