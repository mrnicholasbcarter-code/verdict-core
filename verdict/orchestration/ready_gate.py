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
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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

DEFAULT_CI_DEADLINE_S = 3600.0  # 60 minutes, as the design and the CLI default say
DEFAULT_CI_POLL_INTERVAL_S = 30.0
_FAMILY_LEVEL = "family"
_NO_REVIEWER_MARKER = "no independent reviewer"
_MAX_DETAIL = 500
_CHECK_RUNS_PER_PAGE = 100
_MAX_CHECK_RUN_PAGES = 20
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
class ExtraReviewSummary:
    """What the ready gate's extra cross-family review was and how it ended."""

    route_id: str = ""
    status: str = ""
    findings: int = 0
    blocking: int = 0
    detail: str = ""


@dataclass(frozen=True)
class PrReadyOutcome:
    ready: bool
    reason: PrReadyReason | None = None
    message: str = ""
    review_ran: bool = False
    polls: int = 0
    extra_review: ExtraReviewSummary | None = None


def _stop(
    reason: PrReadyReason,
    message: str,
    *,
    review_ran: bool = False,
    polls: int = 0,
    extra_review: ExtraReviewSummary | None = None,
) -> PrReadyOutcome:
    return PrReadyOutcome(
        False,
        reason,
        redact_text(message)[:_MAX_DETAIL],
        review_ran=review_ran,
        polls=polls,
        extra_review=extra_review,
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
) -> tuple[PrReadyOutcome | None, ExtraReviewSummary | None]:
    """Return ``(stop outcome or None when the review passed, summary or None)``."""
    if reviewer is None:
        return (
            _stop(
                PrReadyReason.NO_CROSS_FAMILY_REVIEWER,
                "in-run review was route-level and no cross-family reviewer is available",
            ),
            None,
        )
    excluded = frozenset(f for f in plan.review.implementer_families if f)
    request = CrossFamilyReviewRequest(head_sha, plan.base_branch, pr_ref, excluded)
    try:
        result = reviewer(request)
    except NoCrossFamilyReviewerError as exc:
        return _stop(
            PrReadyReason.NO_CROSS_FAMILY_REVIEWER, f"no cross-family reviewer: {exc}"
        ), None
    except Exception as exc:  # fail closed: an erroring review is a failed review
        error = ExtraReviewSummary(
            status="ERROR", detail=redact_text(f"{type(exc).__name__}: {exc}")[:_MAX_DETAIL]
        )
        return (
            _stop(
                PrReadyReason.REVIEW_FAILED,
                f"cross-family review errored: {type(exc).__name__}: {exc}",
                review_ran=True,
            ),
            error,
        )
    summary = ExtraReviewSummary(
        route_id=result.route_id,
        status=result.status,
        findings=len(result.findings),
        blocking=sum(1 for f in result.findings if f.blocking()),
        detail=redact_text(result.detail)[:_MAX_DETAIL],
    )
    if result.status == "ERROR" and _NO_REVIEWER_MARKER in result.detail:
        return (
            _stop(PrReadyReason.NO_CROSS_FAMILY_REVIEWER, result.detail, review_ran=True),
            summary,
        )
    if not result.passed:
        return (
            _stop(
                PrReadyReason.REVIEW_FAILED,
                f"cross-family review {result.status}: {result.detail or 'blocking findings'}",
                review_ran=True,
            ),
            summary,
        )
    if not result.route_id.strip():
        return (
            _stop(
                PrReadyReason.REVIEW_FAILED,
                "cross-family review named no reviewer route",
                review_ran=True,
            ),
            summary,
        )
    if route_family(result.route_id) in excluded:
        return (
            _stop(
                PrReadyReason.NO_CROSS_FAMILY_REVIEWER,
                f"reviewer route {result.route_id!r} is in an implementer family",
                review_ran=True,
            ),
            summary,
        )
    return None, summary


def _check_runs_state(runs: Sequence[Any]) -> str:
    """``red``, ``pending``, ``green`` or ``none`` (no check run reported at all)."""
    if not runs:
        return "none"
    state = "green"
    for run in runs:
        if not isinstance(run, Mapping):
            return "pending"
        if run.get("status") != "completed":
            state = "pending"
        elif run.get("conclusion") not in _CHECK_GREEN:
            return "red"
    return state


def _statuses_state(payload: Any) -> str:
    """``red``, ``pending``, ``green`` or ``none`` (no legacy commit status at all)."""
    if not isinstance(payload, Mapping):
        return "pending"
    if not payload.get("total_count") and not payload.get("statuses"):
        return "none"
    state = payload.get("state")
    if state == _STATUS_GREEN:
        return "green"
    return "red" if state in _STATUS_RED else "pending"


def _read_check_runs(
    runner: CommandRunner, head_sha: str, cwd: Path | None
) -> tuple[list[Any], bool, CommandResult | None]:
    """Every check run of the commit, following pages until ``total_count`` is reached.

    Returns ``(runs, complete, failure)``. ``complete`` is false when the server reported
    more runs than it served (or the page cap was hit): the caller must not read that
    as green.
    """
    runs: list[Any] = []
    total = 0
    for page in range(1, _MAX_CHECK_RUN_PAGES + 1):
        payload, failure = _gh_json(
            runner,
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs"
                f"?per_page={_CHECK_RUNS_PER_PAGE}&page={page}",
            ],
            cwd,
        )
        if failure is not None:
            return [], False, failure
        batch = payload.get("check_runs") if isinstance(payload, Mapping) else None
        count = payload.get("total_count") if isinstance(payload, Mapping) else None
        if not isinstance(batch, list) or isinstance(count, bool) or not isinstance(count, int):
            return [], False, CommandResult(1, "", "unexpected check-runs response from gh")
        runs.extend(batch)
        total = count
        if len(runs) >= total or not batch:
            break
    return runs, len(runs) >= total, None


def _poll_checks(
    runner: CommandRunner, head_sha: str, cwd: Path | None
) -> tuple[str, CommandResult | None]:
    """One poll: ``red`` if anything failed, ``green`` only on complete green evidence.

    The two CI mechanisms (check runs and legacy commit statuses) are neutral when one of
    them has no evidence at all; with no evidence in both, the answer is ``pending``.
    """
    runs, complete, failure = _read_check_runs(runner, head_sha, cwd)
    if failure is not None:
        return "pending", failure
    statuses, failure = _gh_json(
        runner, ["gh", "api", f"repos/{{owner}}/{{repo}}/commits/{head_sha}/status"], cwd
    )
    if failure is not None:
        return "pending", failure
    run_state = _check_runs_state(runs)
    if not complete and run_state != "red":
        run_state = "pending"  # the server reported runs we never saw
    states = {run_state, _statuses_state(statuses)}
    if "red" in states:
        return "red", None
    evidence = states - {"none"}
    return ("green" if evidence == {"green"} else "pending"), None


def _verify_head(
    runner: CommandRunner, ref: str, sha: str, cwd: Path | None, *, review_ran: bool, polls: int = 0
) -> PrReadyOutcome | None:
    """Return a stop outcome unless the pull request head is still ``sha``."""
    view, failure = _gh_json(runner, ["gh", "pr", "view", ref, "--json", "headRefOid"], cwd)
    if failure is not None:
        return _stop(
            PrReadyReason.GH_FAILED,
            f"gh pr view failed: {failure.stderr}",
            review_ran=review_ran,
            polls=polls,
        )
    current = str(view.get("headRefOid") or "").lower() if isinstance(view, Mapping) else ""
    if not current or not (current == sha or (len(sha) < 40 and current.startswith(sha))):
        return _stop(
            PrReadyReason.HEAD_MOVED,
            f"pull request head is {current or 'unknown'}, not {sha}",
            review_ran=review_ran,
            polls=polls,
        )
    return None


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
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        # nan and inf never expire (``remaining <= 0`` is never true); zero never waits.
        return _stop(
            PrReadyReason.INVALID_REQUEST,
            f"the checks deadline must be a finite number of seconds above zero, got {deadline_s!r}",
        )

    review_ran = False
    summary: ExtraReviewSummary | None = None
    if plan.review.level != _FAMILY_LEVEL:
        stopped, summary = _run_extra_review(plan, sha, ref, reviewer)
        review_ran = reviewer is not None
        if stopped is not None:
            return replace(stopped, extra_review=summary)

    moved = _verify_head(runner, ref, sha, cwd, review_ran=review_ran)
    if moved is not None:
        return replace(moved, extra_review=summary)

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
                extra_review=summary,
            )
        if state == "red":
            return _stop(
                PrReadyReason.CI_RED,
                f"a check failed on {sha}",
                review_ran=review_ran,
                polls=polls,
                extra_review=summary,
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
                extra_review=summary,
            )
        sleep(min(poll_interval_s, remaining))

    # The wait may have lasted up to the deadline: promote only the commit that was checked.
    moved = _verify_head(runner, ref, sha, cwd, review_ran=review_ran, polls=polls)
    if moved is not None:
        return replace(moved, extra_review=summary)
    ready = runner(["gh", "pr", "ready", ref], cwd)
    if ready.returncode != 0:
        return _stop(
            PrReadyReason.GH_FAILED,
            f"gh pr ready failed: {ready.stderr or ready.stdout}",
            review_ran=review_ran,
            polls=polls,
            extra_review=summary,
        )
    return PrReadyOutcome(True, None, "pull request marked ready", review_ran, polls, summary)
