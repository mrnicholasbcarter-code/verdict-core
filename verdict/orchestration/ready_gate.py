"""Ready-gate: per-story READY / EXCLUDED / WAIT decision (BOD-157 AC4/AC5).

Pure library — deterministic, no network, no Linear calls.
All inputs are typed values; unknown or missing evidence fails closed (WAIT).

The second half of the module is the pull-request ready gate of ``verdict deliver``
(``run_pr_ready_gate``): an extra cross-family review when the in-run review was
only route-level, a wait for checks on the exact head SHA, then ``gh pr ready``.
Git, ``gh``, the reviewer, the clock and sleep are injected. It never merges,
approves, closes or labels anything. See the ``verdict-deliver`` OpenSpec change.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import ReviewResult, route_family
from verdict.orchestration.deliver import DeliverPlan
from verdict.orchestration.deliver_publish import CommandResult, CommandRunner, run_command
from verdict.security import redact_text


class ReadyVerdict(str, Enum):
    """Outcome of a ready-gate evaluation."""

    READY = "READY"
    EXCLUDED = "EXCLUDED"
    WAIT = "WAIT"


@dataclass(frozen=True)
class DepEvidence:
    """Evidence about a single dependency story.

    Parameters
    ----------
    identifier:
        Opaque dependency identifier (e.g. issue key).
    linear_done:
        Whether Linear reports this dependency as Done/Completed.
        ``None`` means unknown.
    merge_commit_on_main:
        Whether the dependency's merge commit is an ancestor of *main_sha*.
        ``None`` means unknown / not checked.
    verification_record_present:
        Whether a verification record exists for this dependency.
        ``None`` means unknown / not checked.
    """

    identifier: str
    linear_done: bool | None = None
    merge_commit_on_main: bool | None = None
    verification_record_present: bool | None = None

    @property
    def main_verified(self) -> bool:
        """True only when BOTH merge-on-main AND verification record are confirmed."""
        return self.merge_commit_on_main is True and self.verification_record_present is True


@dataclass(frozen=True)
class ReadyDecision:
    """Result of :func:`ready_decision`."""

    verdict: ReadyVerdict
    reason: str


def ready_decision(
    *, labels: frozenset[str], deps: Sequence[DepEvidence], main_sha: str
) -> ReadyDecision:
    """Decide whether a story is READY for concurrent dispatch.

    Parameters
    ----------
    labels:
        Set of labels on the story (e.g. from Linear).
    deps:
        Evidence records for each dependency.
    main_sha:
        Current main branch HEAD SHA (used only as documentation; ancestry
        checks are pre-computed in *deps*).

    Returns
    -------
    ReadyDecision
        ``EXCLUDED`` if the story carries ``automation:manual``.
        ``WAIT`` if any dependency is not MAIN_VERIFIED or has unknown evidence.
        ``READY`` otherwise.

    The function is deterministic and performs no I/O.
    """
    # --- Rule 1: manual label → EXCLUDED ----------------------------------
    if "automation:manual" in labels:
        return ReadyDecision(
            verdict=ReadyVerdict.EXCLUDED,
            reason="label 'automation:manual' present; story excluded from auto-dispatch",
        )

    # --- Rule 2: no deps → READY ------------------------------------------
    if not deps:
        return ReadyDecision(verdict=ReadyVerdict.READY, reason="no dependencies; story is ready")

    # --- Rule 3: check each dependency ------------------------------------
    waiting: list[str] = []
    for dep in deps:
        if dep.main_verified:
            continue  # this dependency is satisfied

        # Distinguish "done in Linear but not verified on main" from unknown.
        if dep.linear_done is True:
            waiting.append(f"dep '{dep.identifier}' is Done in Linear but not MAIN_VERIFIED")
        elif dep.merge_commit_on_main is None or dep.verification_record_present is None:
            waiting.append(
                f"dep '{dep.identifier}' has unknown/missing evidence; fail-closed to WAIT"
            )
        else:
            waiting.append(f"dep '{dep.identifier}' is not MAIN_VERIFIED")

    if waiting:
        return ReadyDecision(verdict=ReadyVerdict.WAIT, reason="; ".join(waiting))

    from verdict.operator_policy import ActionContext, PolicyUnavailableError, admit_action

    try:
        verdict, _audit = admit_action(
            ActionContext(action="story.ready", argv=("verdict", "ready"), linear_issue=None)
        )
    except PolicyUnavailableError as exc:
        return ReadyDecision(
            verdict=ReadyVerdict.WAIT, reason=f"operator policy unavailable: {exc}"
        )
    if not verdict.allowed:
        return ReadyDecision(
            verdict=ReadyVerdict.WAIT, reason=f"operator policy denied: {verdict.reason}"
        )
    return ReadyDecision(
        verdict=ReadyVerdict.READY, reason="all dependencies are MAIN_VERIFIED; story is ready"
    )


# --- Pull-request ready gate (verdict deliver) ---------------------------------

DEFAULT_CI_DEADLINE_S = 1800.0
DEFAULT_CI_POLL_INTERVAL_S = 30.0
_FAMILY_LEVEL = "family"
_NO_REVIEWER_MARKER = "no independent reviewer"
_MAX_DETAIL = 500
_CHECK_GREEN = frozenset({"success", "neutral", "skipped"})
_STATUS_GREEN = "success"
_STATUS_RED = frozenset({"failure", "error"})


class PrReadyReason(str, Enum):
    """Stable stop codes of the ready gate. A stop leaves the pull request a draft."""

    NO_CROSS_FAMILY_REVIEWER = "no_cross_family_reviewer"
    REVIEW_FAILED = "review_failed"
    CI_RED = "ci_red"
    CI_PENDING = "ci_pending"
    HEAD_MOVED = "head_moved"
    INVALID_REQUEST = "invalid_request"
    GH_FAILED = "gh_failed"


class NoCrossFamilyReviewerError(Exception):
    """Raised by a reviewer seam when no reviewer of another family is available."""


@dataclass(frozen=True)
class CrossFamilyReviewRequest:
    """What the injected reviewer is asked to review."""

    head_sha: str
    base_branch: str
    pr_ref: str
    exclude_families: frozenset[str]


CrossFamilyReviewer = Callable[[CrossFamilyReviewRequest], ReviewResult]


@dataclass(frozen=True)
class PrReadyOutcome:
    ready: bool
    reason: PrReadyReason | None = None
    message: str = ""
    review_ran: bool = False
    polls: int = 0


def _stop(
    reason: PrReadyReason, message: str, *, review_ran: bool = False, polls: int = 0
) -> PrReadyOutcome:
    return PrReadyOutcome(
        False, reason, redact_text(message)[:_MAX_DETAIL], review_ran=review_ran, polls=polls
    )


def _gh_json(
    runner: CommandRunner, argv: list[str], cwd: Path | None
) -> tuple[Any, CommandResult | None]:
    result = runner(argv, cwd)
    if result.returncode != 0:
        return None, result
    try:
        return json.loads(result.stdout or "null"), None
    except json.JSONDecodeError:
        return None, CommandResult(1, "", "invalid JSON from gh")


def _run_extra_review(
    plan: DeliverPlan, head_sha: str, pr_ref: str, reviewer: CrossFamilyReviewer | None
) -> PrReadyOutcome | None:
    """Return a stop outcome, or ``None`` when the cross-family review passed."""
    if reviewer is None:
        return _stop(
            PrReadyReason.NO_CROSS_FAMILY_REVIEWER,
            "in-run review was route-level and no cross-family reviewer is available",
        )
    excluded = frozenset(f for f in plan.review.implementer_families if f)
    request = CrossFamilyReviewRequest(head_sha, plan.base_branch, pr_ref, excluded)
    try:
        result = reviewer(request)
    except NoCrossFamilyReviewerError as exc:
        return _stop(PrReadyReason.NO_CROSS_FAMILY_REVIEWER, f"no cross-family reviewer: {exc}")
    except Exception as exc:  # fail closed: an erroring review is a failed review
        return _stop(
            PrReadyReason.REVIEW_FAILED,
            f"cross-family review errored: {type(exc).__name__}: {exc}",
            review_ran=True,
        )
    if result.status == "ERROR" and _NO_REVIEWER_MARKER in result.detail:
        return _stop(PrReadyReason.NO_CROSS_FAMILY_REVIEWER, result.detail, review_ran=True)
    if not result.passed:
        return _stop(
            PrReadyReason.REVIEW_FAILED,
            f"cross-family review {result.status}: {result.detail or 'blocking findings'}",
            review_ran=True,
        )
    if result.route_id and route_family(result.route_id) in excluded:
        return _stop(
            PrReadyReason.NO_CROSS_FAMILY_REVIEWER,
            f"reviewer route {result.route_id!r} is in an implementer family",
            review_ran=True,
        )
    return None


def _check_runs_state(payload: Any) -> str:
    runs = payload.get("check_runs") if isinstance(payload, Mapping) else None
    if not isinstance(runs, list):
        return "pending"
    state = "green" if runs else "pending"
    for run in runs:
        if not isinstance(run, Mapping):
            return "pending"
        if run.get("status") != "completed":
            state = "pending"
        elif run.get("conclusion") not in _CHECK_GREEN:
            return "red"
    return state


def _statuses_state(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "pending"
    if not payload.get("total_count") and not payload.get("statuses"):
        return "green"  # no commit statuses at all is not a reason to wait
    state = payload.get("state")
    if state == _STATUS_GREEN:
        return "green"
    return "red" if state in _STATUS_RED else "pending"


def _poll_checks(
    runner: CommandRunner, head_sha: str, cwd: Path | None
) -> tuple[str, CommandResult | None]:
    runs, failure = _gh_json(
        runner,
        ["gh", "api", f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs?per_page=100"],
        cwd,
    )
    if failure is not None:
        return "pending", failure
    statuses, failure = _gh_json(
        runner, ["gh", "api", f"repos/{{owner}}/{{repo}}/commits/{head_sha}/status"], cwd
    )
    if failure is not None:
        return "pending", failure
    states = {_check_runs_state(runs), _statuses_state(statuses)}
    if "red" in states:
        return "red", None
    return ("green" if states == {"green"} else "pending"), None


def run_pr_ready_gate(
    plan: DeliverPlan,
    *,
    pr_ref: str,
    head_sha: str,
    reviewer: CrossFamilyReviewer | None = None,
    runner: CommandRunner = run_command,
    cwd: Path | None = None,
    deadline_s: float = DEFAULT_CI_DEADLINE_S,
    poll_interval_s: float = DEFAULT_CI_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PrReadyOutcome:
    """Mark the draft pull request ready only when review and checks are green.

    Order: (1) read the in-run review level from ``plan``; (2) when it was not
    family-level (route-level or unknown), run the extra cross-family review;
    (3) wait for checks on exactly ``head_sha``; (4) ``gh pr ready``. Every stop
    leaves the pull request a draft.
    """
    ref = pr_ref.strip()
    sha = head_sha.strip().lower()
    if not ref or not sha or not all(c in "0123456789abcdef" for c in sha):
        return _stop(
            PrReadyReason.INVALID_REQUEST, "a pull request reference and head sha are required"
        )

    review_ran = False
    if plan.review.level != _FAMILY_LEVEL:
        stopped = _run_extra_review(plan, sha, ref, reviewer)
        review_ran = reviewer is not None
        if stopped is not None:
            return stopped

    view, failure = _gh_json(runner, ["gh", "pr", "view", ref, "--json", "headRefOid"], cwd)
    if failure is not None:
        return _stop(
            PrReadyReason.GH_FAILED, f"gh pr view failed: {failure.stderr}", review_ran=review_ran
        )
    current = str(view.get("headRefOid") or "").lower() if isinstance(view, Mapping) else ""
    if not current or not (current == sha or current.startswith(sha) or sha.startswith(current)):
        return _stop(
            PrReadyReason.HEAD_MOVED,
            f"pull request head is {current or 'unknown'}, not {sha}",
            review_ran=review_ran,
        )

    deadline = clock() + deadline_s
    polls = 0
    while True:
        polls += 1
        state, failure = _poll_checks(runner, sha, cwd)
        if failure is not None:
            return _stop(
                PrReadyReason.GH_FAILED,
                f"gh api checks failed: {failure.stderr}",
                review_ran=review_ran,
                polls=polls,
            )
        if state == "red":
            return _stop(
                PrReadyReason.CI_RED, f"a check failed on {sha}", review_ran=review_ran, polls=polls
            )
        if state == "green":
            break
        remaining = deadline - clock()
        if remaining <= 0:
            return _stop(
                PrReadyReason.CI_PENDING,
                f"checks on {sha} still pending after {deadline_s:g}s",
                review_ran=review_ran,
                polls=polls,
            )
        sleep(min(poll_interval_s, remaining))

    ready = runner(["gh", "pr", "ready", ref], cwd)
    if ready.returncode != 0:
        return _stop(
            PrReadyReason.GH_FAILED,
            f"gh pr ready failed: {ready.stderr or ready.stdout}",
            review_ran=review_ran,
            polls=polls,
        )
    return PrReadyOutcome(True, None, "pull request marked ready", review_ran, polls)
