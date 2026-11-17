"""PR watcher core: exact-head polling, action adaptation, and reconciliation.

Pure orchestration: loads GitHub facts, classifies PRs, applies bounded
recovery, writes durable state before side effects, and emits transition
notifications only.  No LLM or model-routing calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.ci_policy import (
    INFRA_CONCLUSIONS,
    REPAIR_CONCLUSIONS,
    classify_ci,
    classify_merge_state,
)
from verdict.pr_watcher_github import GitHubClientError, WatcherGitHubClient
from verdict.pr_watcher_github import assert_no_mutation as assert_github_no_mutation
from verdict.pr_watcher_notifications import (
    LinearClient,
    build_digest_event,
    build_linear_event,
    deliver_linear_events,
    transition_event_type,
)
from verdict.pr_watcher_notifications import assert_no_mutation as assert_linear_no_mutation
from verdict.pr_watcher_state import (
    STALLED_THRESHOLD_MINUTES,
    BlockRecord,
    LockAcquisitionError,
    MalformedStateError,
    RegressionRecord,
    RepairRecord,
    StateWriteError,
    UnsafeKeyError,
    WatcherState,
    format_timestamp,
    rerun_key,
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

# Required-check conclusions a bounded rerun may clear, and those a repair names.
_RERUNNABLE_CONCLUSIONS = frozenset({"FAILURE", "CANCELLED"}) | INFRA_CONCLUSIONS
_FAILING_CONCLUSIONS = frozenset({"FAILURE"}) | REPAIR_CONCLUSIONS


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
    if classification in _BLOCKED:
        return "BLOCKED"
    return "NO_ACTION"


# --- Core polling ---------------------------------------------------------------


def _resolve_clock(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("injected clock must be timezone-aware")
    return current


def poll_open_prs(
    gh: WatcherGitHubClient, state: WatcherState, repo: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Classify every open PR at its exact head SHA.

    Returns a list of observations with classification, action, and state.
    """
    current = _resolve_clock(now)
    observations: list[dict[str, Any]] = []
    prs = gh.list_open_prs(repo)

    for listed in prs:
        head = listed["head_sha"]
        pr_num = listed["number"]

        # One read returns the PR head, the commit its checks belong to and
        # the checks themselves, so the three cannot drift apart.
        pr = {**listed, **gh.get_pr(repo, pr_num)}
        checks = pr["checks"]

        # Exact head: the checks are acted on only if they belong to the head
        # that was listed. Otherwise the PR is reported and left for the next
        # run, with no record, rerun or notification.
        if pr["head_sha"] != head or pr.get("commit_oid") != head:
            observations.append(
                {
                    "repo": repo,
                    "pr_number": pr_num,
                    "head": head,
                    "ci_classification": "HEAD_CHANGED",
                    "merge_classification": "UNKNOWN",
                    "classification": "HEAD_CHANGED",
                    "action": "NO_ACTION",
                    "checks": [],
                    "rerun_targets": [],
                    "merge_state": pr.get("merge_state_status", ""),
                    "linear_issue": pr.get("linear_issue"),
                }
            )
            continue

        # Only the checks branch protection requires decide the result; the
        # client reports that flag per check.
        required_checks = [c for c in checks if c.get("required") is True]

        # Stalled detection: >30 minutes in progress
        stalled = [c for c in required_checks if _is_stalled(c, current)]

        # CI classification (classify_ci itself ignores optional checks)
        ci_class = classify_ci(checks)
        if stalled:
            ci_class = "STALLED"  # override to stalled when checks hang
        # Only the required checks that actually hung or failed are rerun candidates.
        rerun_targets = stalled or [
            c for c in required_checks if c.get("conclusion") in _RERUNNABLE_CONCLUSIONS
        ]

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
            "checks": checks,
            "rerun_targets": rerun_targets,
            "merge_state": pr.get("merge_state_status", ""),
            "linear_issue": pr.get("linear_issue"),
        }
        observations.append(obs)

    return observations


def _is_stalled(check: dict[str, Any], current: datetime) -> bool:
    """True if this check has been queued or in progress for over the threshold."""
    if check.get("status") not in {"IN_PROGRESS", "QUEUED"}:
        return False
    started_at = check.get("started_at")
    if started_at is None:
        return False
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None or started.utcoffset() is None:
        return False
    return (current - started).total_seconds() > STALLED_THRESHOLD_MINUTES * 60


def _combine_classifications(ci: str, merge: str) -> str:
    """Combine CI and merge classifications into a single result.

    CONFLICT and BEHIND outrank the CI result.  GREEN stands only when GitHub
    reports the PR mergeable and clean; while mergeability is unknown or held
    back the PR is PENDING, which is neither ready nor a repair.
    """
    if merge in {"CONFLICT", "BEHIND"}:
        return merge
    if ci == "GREEN" and merge != "MERGEABLE":
        return "PENDING"
    return ci


# --- Merged PR reconciliation ---------------------------------------------------

# Re-list this far behind the cursor: GitHub search lags merges, and a merge
# can be stamped slightly before the run that should have seen it.
MERGED_LOOKBACK = timedelta(minutes=5)
# Window for a run with no usable cursor, so the first query is never unbounded.
MERGED_INITIAL_LOOKBACK = timedelta(hours=24)


def _merged_since(cursor: datetime | None, now: datetime) -> str:
    """Lower bound for the merged-PR query; always a concrete instant."""
    # A cursor ahead of the clock would hide every merge until the clock
    # caught up, so it is treated like a missing one.
    if cursor is None or cursor > now:
        return format_timestamp(now - MERGED_INITIAL_LOOKBACK)
    return format_timestamp(cursor - MERGED_LOOKBACK)


def _has_standing_change_request(reviews: list[dict[str, Any]], head: str) -> bool:
    """True if a CHANGES_REQUESTED review is a fail on the merged head.

    It is when it was made on the merged head itself, or when no later
    APPROVED review of that head exists.  A review that does not say which
    commit it was made on cannot be shown to be older, so it counts.
    """
    # Oldest first: by submission time when the client reports one, otherwise
    # in the order the client returned them.
    ordered = [
        review
        for _, review in sorted(
            enumerate(reviews), key=lambda item: (item[1].get("submitted_at") or "", item[0])
        )
    ]
    for position, review in enumerate(ordered):
        if review["state"] != "CHANGES_REQUESTED":
            continue
        if review.get("commit_oid", head) == head:
            return True
        approved_later = any(
            later["state"] == "APPROVED" and later.get("commit_oid") == head
            for later in ordered[position + 1 :]
        )
        if not approved_later:
            return True
    return False


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
    clock = _resolve_clock(now)
    since = _merged_since(state.get_merged_cursor(current_data, repo), clock)
    merged_prs = gh.list_merged_prs(repo, since)

    regressions: list[dict[str, Any]] = []
    verdict_fail = verdict_fail_receipts or {}

    for pr in merged_prs:
        pr_num = pr["number"]
        head = pr["head_sha"]
        reviews = pr["reviews"]

        has_changes_requested = _has_standing_change_request(reviews, head)

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
            regressions.append(
                {
                    "record": rec.to_dict(format_timestamp(clock)),
                    "pr": pr,
                    "review_sources": fail_evidence,
                }
            )

    return regressions


# --- Bounded rerun with fencing --------------------------------------------------


def _still_rerunnable(
    checks: list[dict[str, Any]], check_name: str, workflow_job_id: object, stalled: bool
) -> bool:
    """True if the same required check, by name and job id, still warrants the rerun."""
    for check in checks:
        if check.get("name") != check_name or check.get("job_id") != workflow_job_id:
            continue
        if check.get("required") is not True:
            return False
        if stalled:
            return check.get("status") in {"IN_PROGRESS", "QUEUED"}
        return check.get("conclusion") in _RERUNNABLE_CONCLUSIONS
    return False


def request_bounded_rerun(
    gh: WatcherGitHubClient,
    state: WatcherState,
    repo: str,
    pr_number: int,
    head: str,
    check_name: str,
    workflow_job_id: int | str,
    max_attempts: int = 1,
    data: dict[str, Any] | None = None,
    stalled: bool = False,
) -> dict[str, Any]:
    """Request one bounded rerun, fenced on the exact head and check identity.

    Order: reread the PR, confirm the head and that the same check (name and
    job id) is still failed or, for ``stalled``, still unfinished; check the
    per-key budget; persist the raised attempt counter; only then call GitHub.
    A fence mismatch consumes no attempt.  ``data``, when given, is updated in
    place so the caller keeps the state that is now on disk.  A failed state
    write raises ``StateWriteError`` and no rerun is requested.
    """
    if data is None:
        data = state.load()
    key = rerun_key(repo, pr_number, head, check_name, workflow_job_id)

    try:
        reread = gh.get_pr(repo, pr_number)
    except GitHubClientError as exc:
        # Unfenced means not rerun: nothing is spent and the next run retries.
        return {"rerun": False, "reason": str(exc), "type": "read_error"}
    current_head = reread["head_sha"]
    if current_head != head or reread.get("commit_oid") != head:
        return {
            "rerun": False,
            "reason": f"head changed: expected {head[:12]}, got {current_head[:12]}",
            "type": "fence_mismatch",
        }
    if not _still_rerunnable(reread["checks"], check_name, workflow_job_id, stalled):
        return {
            "rerun": False,
            "reason": f"check {check_name!r} (job {workflow_job_id}) is no longer rerunnable",
            "type": "fence_mismatch",
        }

    attempt = state.get_rerun_attempt(data, key)
    if attempt >= max_attempts:
        return {
            "rerun": False,
            "reason": f"max attempts ({max_attempts}) exhausted for {key}",
            "type": "exhausted",
        }

    data.update(state.set_rerun_attempt(data, key, attempt + 1))
    state.save(data)

    # The attempt is already spent on disk, so a failed or interrupted call
    # can never lead to a second rerun for this key.
    try:
        result = gh.rerun_check(workflow_job_id, repo)
    except Exception as exc:
        return {"rerun": False, "reason": str(exc), "type": "api_error"}
    return {"rerun": True, "result": result}


# --- Run coordinator -------------------------------------------------------------


@dataclass
class _Plan:
    """What one run has decided, before anything is written or sent."""

    data: dict[str, Any]
    linear_enabled: bool
    stamp: str
    records: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    reruns: list[tuple[str, int, str, str, Any, bool]] = field(default_factory=list)

    def fork(self) -> _Plan:
        """A plan that starts from this one's state and can be thrown away."""
        return _Plan(data=self.data, linear_enabled=self.linear_enabled, stamp=self.stamp)

    def adopt(self, other: _Plan) -> None:
        self.data = other.data
        self.records.extend(other.records)
        self.notifications.extend(other.notifications)
        self.reruns.extend(other.reruns)

    def add_record(self, state: WatcherState, record: dict[str, Any]) -> None:
        if not state.has_record(self.data, record["idempotency_key"]):
            self.data = state.add_record(self.data, record)
            self.records.append(record)

    def emit(self, state: WatcherState, event: dict[str, Any], linear_issue: object) -> None:
        """Record a digest event once, and queue the linked story's Linear comment.

        The durable idempotency key is what makes this transition-only: the
        same transition on the same head is already recorded and emits nothing.
        """
        if state.has_digest_event(self.data, event["idempotency_key"]):
            return
        self.data = state.add_digest_event(self.data, event)
        self.notifications.append(event)
        if self.linear_enabled and isinstance(linear_issue, str) and linear_issue:
            self.data = state.add_pending_event(self.data, build_linear_event(linear_issue, event))


def _plan_observation(
    state: WatcherState, plan: _Plan, obs: dict[str, Any], max_attempts: int
) -> None:
    """Turn one open-PR observation into records, a transition and planned reruns."""
    repo, pr_number, head = obs["repo"], obs["pr_number"], obs["head"]
    classification = obs["classification"]
    if classification == "HEAD_CHANGED":
        # Nothing is known about the listed head, so nothing is stored for it.
        return
    previous = state.get_pr_state(plan.data, repo, pr_number, head)
    action = obs["action"]

    if action == "RETRY":
        # One bounded rerun per key: every hung or failed check still inside
        # its own persisted budget is rerun once. The action is BLOCKED only
        # when no key has budget left, which holds on every later run too.
        keyed = [
            (rerun_key(repo, pr_number, head, c["name"], c.get("job_id", "")), c)
            for c in obs["rerun_targets"]
        ]
        within_budget = [
            c for key, c in keyed if state.get_rerun_attempt(plan.data, key) < max_attempts
        ]
        action = _adapter_action(classification, 0 if within_budget else max_attempts, max_attempts)
        obs["action"] = action
        stalled = obs["ci_classification"] == "STALLED"
        plan.reruns.extend(
            (repo, pr_number, head, c["name"], c.get("job_id", ""), stalled) for c in within_budget
        )

    if action == "REPAIR":
        failed = [
            c
            for c in obs["checks"]
            if c.get("required") is True and c.get("conclusion") in _FAILING_CONCLUSIONS
        ]
        failing = [c["name"] for c in failed]
        details = [
            {
                "name": c["name"],
                "conclusion": c["conclusion"],
                "job_id": c.get("job_id"),
                "details_url": c.get("details_url"),
            }
            for c in failed
        ]
        # Name the conclusion when it is not a plain failure, so the repair
        # worker can tell "needs a person" from "the code is wrong".
        named = sorted({str(c["conclusion"]) for c in failed} & REPAIR_CONCLUSIONS)
        reason = (
            f"required check concluded {', '.join(named)}"
            if named
            else f"PR classified as {classification}"
        )
        plan.add_record(
            state,
            RepairRecord(
                repo=repo,
                pr_number=pr_number,
                head=head,
                failing_checks=failing,
                failing_check_details=details,
                classification=classification,
                reason=reason,
            ).to_dict(plan.stamp),
        )
    elif action == "BLOCKED":
        plan.add_record(
            state,
            BlockRecord(repo, pr_number, head, f"attempts exhausted for {classification}").to_dict(
                plan.stamp
            ),
        )

    plan.data = state.set_pr_state(plan.data, repo, pr_number, head, classification, action=action)

    event_type = transition_event_type(action)
    if event_type is None:
        return
    if action == "BLOCKED":
        event_class, reason = "BLOCKED", f"attempts exhausted for {classification}"
    else:
        seen_before = previous["classification"] if previous else "first observation"
        event_class, reason = classification, f"transitioned from {seen_before} to {classification}"
    plan.emit(
        state,
        build_digest_event(event_type, repo, pr_number, head, event_class, reason, plan.stamp),
        obs["linear_issue"],
    )


def _plan_repository(
    gh: WatcherGitHubClient,
    state: WatcherState,
    plan: _Plan,
    repo: str,
    max_attempts: int,
    verdict_fail_receipts: dict[str, dict[str, Any]] | None,
    current: datetime,
) -> list[dict[str, Any]]:
    """Decide everything for one repository. Only reads happen here."""
    polled = poll_open_prs(gh, state, repo, now=current)
    for obs in polled:
        _plan_observation(state, plan, obs, max_attempts)

    for reg in reconcile_merged_prs(gh, state, repo, verdict_fail_receipts, plan.data, current):
        plan.add_record(state, reg["record"])
        sources = ", ".join(reg["review_sources"])
        plan.emit(
            state,
            build_digest_event(
                "regression",
                repo,
                reg["pr"]["number"],
                reg["pr"]["head_sha"],
                "REGRESSION",
                f"merged with fail review: {sources}",
                plan.stamp,
            ),
            reg["pr"].get("linear_issue"),
        )

    # Open and merged PRs were both read in full, so this repository's cursor
    # may move.
    plan.data = state.set_merged_cursor(plan.data, repo, current)
    return polled


def _run_locked(
    gh: WatcherGitHubClient,
    state: WatcherState,
    data: dict[str, Any],
    repos: list[str],
    linear: LinearClient | None,
    max_attempts: int,
    verdict_fail_receipts: dict[str, dict[str, Any]] | None,
    current: datetime,
) -> dict[str, Any]:
    plan = _Plan(data=data, linear_enabled=linear is not None, stamp=format_timestamp(current))
    observations: list[dict[str, Any]] = []
    repo_errors: list[dict[str, str]] = []

    # 1. Decide, one repository at a time. A repository whose reads fail is
    # dropped as a whole: no record, transition or cursor move is kept for it,
    # so the next run covers the same ground. The others carry on.
    for repo in repos:
        trial = plan.fork()
        try:
            polled = _plan_repository(
                gh, state, trial, repo, max_attempts, verdict_fail_receipts, current
            )
        except GitHubClientError as exc:
            repo_errors.append({"repo": repo, "error": str(exc)})
            continue
        plan.adopt(trial)
        observations.extend(polled)

    # 2. Persist. Classifications, records, transitions, queued Linear events
    # and the cursors are durable before the first side effect.
    data = state.set_last_run(plan.data, current)
    state.save(data)

    # The digest file is appended from that durable state, so a line lost to
    # an interrupted run is written by the next one, and never twice.
    owed = state.unwritten_digest_events(data)
    if owed:
        state.append_digest(owed)
        data = {**data, "digest_unwritten": []}
        state.save(data)

    # 3. Act. Each rerun persists its own attempt counter before its call.
    reruns = []
    for planned in plan.reruns:
        outcome = request_bounded_rerun(gh, state, *planned[:5], max_attempts, data, planned[5])
        reruns.append(outcome)
        if outcome.get("type") in {"read_error", "api_error"}:
            repo_errors.append({"repo": planned[0], "error": outcome["reason"]})

    # Linear comments are sent only from the durable queue, so a failed or
    # interrupted send is retried by the next run.
    sent: list[dict[str, Any]] = []
    if linear is not None:
        pending = state.get_pending_events(data)
        remaining, sent = deliver_linear_events(pending, linear)
        if remaining != pending:
            data = {**data, "pending_events": remaining}
            state.save(data)

    return {
        "ok": not repo_errors,
        "repo_errors": repo_errors,
        "observations": observations,
        "reruns": reruns,
        "records": plan.records,
        "notifications": plan.notifications,
        "linear_sent": sent,
        "pending_events": state.get_pending_events(data),
    }


def run_watcher(
    gh: WatcherGitHubClient,
    state: WatcherState,
    repos: list[str],
    linear: LinearClient | None = None,
    max_attempts: int = 1,
    verdict_fail_receipts: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute one watcher run: poll, classify, adapt, persist, then act.

    Durable state is written before any GitHub or Linear side effect.  A run
    that is given a client it must refuse, or cannot take the lock, read its
    state, build a safe key or write its state, returns ``ok`` False with an
    ``error`` and a ``type`` and causes no side effect.  A GitHub failure in
    one repository is listed in ``repo_errors`` and also makes ``ok`` False,
    while the other repositories are still handled.  No LLM or model-routing
    call is made.
    """
    current = _resolve_clock(now)
    try:
        assert_github_no_mutation(gh)
        if linear is not None:
            assert_linear_no_mutation(linear)
    except TypeError as exc:
        return {"ok": False, "error": str(exc), "type": "client_refused"}
    try:
        state.acquire_lock()
    except (LockAcquisitionError, OSError) as exc:
        return {"ok": False, "error": str(exc), "type": "lock_failure"}

    try:
        try:
            data = state.load()
        except MalformedStateError as exc:
            return {"ok": False, "error": str(exc), "type": "malformed_state"}
        try:
            return _run_locked(
                gh, state, data, repos, linear, max_attempts, verdict_fail_receipts, current
            )
        except UnsafeKeyError as exc:
            return {"ok": False, "error": str(exc), "type": "unsafe_state_key"}
        except StateWriteError as exc:
            return {"ok": False, "error": str(exc), "type": "state_write_failed"}
    finally:
        state.release_lock()
