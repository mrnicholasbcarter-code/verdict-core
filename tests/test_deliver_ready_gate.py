"""Tests for the pull-request ready gate (``run_pr_ready_gate``).

git, ``gh``, the cross-family reviewer, the clock and sleep are all fakes. Nothing
live runs and no real home directory is touched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_deliver_publish import _plan
from verdict.orchestration import ready_gate
from verdict.orchestration.contracts import ReviewFinding, ReviewResult
from verdict.orchestration.deliver import DeliverPlan
from verdict.orchestration.deliver_publish import CommandResult
from verdict.orchestration.ready_gate import (
    CrossFamilyReviewRequest,
    NoCrossFamilyReviewerError,
    PrReadyReason,
    run_pr_ready_gate,
)

HEAD = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
PR = "https://github.com/acme/repo/pull/77"
FORBIDDEN_VERBS = {"merge", "approve", "close", "label", "edit", "comment", "review"}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


def _plan_at(level: str) -> DeliverPlan:
    plan = _plan()
    return replace(plan, review=replace(plan.review, level=level, implementer_families=("claude",)))


def _run(conclusion: str | None, status: str = "completed") -> dict[str, Any]:
    return {"name": "ci", "status": status, "conclusion": conclusion}


class FakeGh:
    """Records every argv; check polls pop from ``polls`` (the last one repeats)."""

    def __init__(
        self,
        polls: Sequence[list[dict[str, Any]]] = ((_run("success"),),),  # type: ignore[assignment]
        *,
        head: str = HEAD,
        heads: Sequence[str] | None = None,
        ready_rc: int = 0,
        statuses: dict[str, Any] | None = None,
        total_count: int | None = None,
        page_size: int = 100,
    ) -> None:
        self.calls: list[list[str]] = []
        self.polls = [list(p) for p in polls]
        self.head = head
        # One head per ``gh pr view``; the last repeats. Lets a test move the head mid-run.
        self.heads = list(heads) if heads is not None else None
        self.ready_rc = ready_rc
        self.statuses = statuses if statuses is not None else {"state": "success", "total_count": 0}
        self.total_count = total_count
        self.page_size = page_size
        self._current_poll: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        call = list(argv)
        self.calls.append(call)
        if call[:3] == ["gh", "pr", "view"]:
            head = self.head
            if self.heads is not None:
                head = self.heads[0] if len(self.heads) == 1 else self.heads.pop(0)
            return CommandResult(0, json.dumps({"headRefOid": head}))
        if call[:2] == ["gh", "api"] and call[2].endswith("/status"):
            return CommandResult(0, json.dumps(self.statuses))
        if call[:2] == ["gh", "api"] and "/check-runs" in call[2]:
            found = re.search(r"[?&]page=(\d+)", call[2])  # not the "page=" inside per_page=
            page = int(found.group(1)) if found else 1
            if page == 1:
                self._current_poll = self.polls[0] if len(self.polls) == 1 else self.polls.pop(0)
            start = (page - 1) * self.page_size
            runs = self._current_poll[start : start + self.page_size]
            total = len(self._current_poll) if self.total_count is None else self.total_count
            return CommandResult(0, json.dumps({"total_count": total, "check_runs": runs}))
        if call[:3] == ["gh", "pr", "ready"]:
            return CommandResult(self.ready_rc, "", "boom" if self.ready_rc else "")
        return CommandResult(0, "")

    def calls_with(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


class FakeReviewer:
    def __init__(self, result: ReviewResult | Exception | None = None) -> None:
        self.requests: list[CrossFamilyReviewRequest] = []
        self.result = result or ReviewResult("PASS", "ocr", "gm/gemini-3")

    def __call__(self, request: CrossFamilyReviewRequest) -> ReviewResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _gate(
    plan: DeliverPlan,
    gh: FakeGh,
    reviewer: FakeReviewer | None = None,
    *,
    time_: FakeTime | None = None,
    deadline_s: float = 120.0,
    poll_interval_s: float = 30.0,
) -> ready_gate.PrReadyOutcome:
    t = time_ or FakeTime()
    return run_pr_ready_gate(
        plan,
        pr_ref=PR,
        head_sha=HEAD,
        reviewer=reviewer,
        runner=gh,
        deadline_s=deadline_s,
        poll_interval_s=poll_interval_s,
        clock=t.clock,
        sleep=t.sleep,
    )


def test_family_level_review_skips_extra_review_and_goes_to_ci() -> None:
    gh, reviewer = FakeGh(), FakeReviewer()
    outcome = _gate(_plan_at("family"), gh, reviewer)
    assert reviewer.requests == []
    assert not outcome.review_ran
    assert outcome.ready
    assert len(gh.calls_with("gh", "pr", "ready")) == 1


def test_route_level_review_runs_extra_review_once_then_ci() -> None:
    gh, reviewer = FakeGh(), FakeReviewer()
    outcome = _gate(_plan_at("route"), gh, reviewer)
    assert len(reviewer.requests) == 1
    request = reviewer.requests[0]
    assert request.head_sha == HEAD
    assert request.exclude_families == frozenset({"claude"})
    assert outcome.review_ran
    assert outcome.ready
    assert gh.calls_with("gh", "api")


def test_route_level_without_reviewer_stops_and_never_readies() -> None:
    gh = FakeGh()
    outcome = _gate(_plan_at("route"), gh, None)
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.NO_CROSS_FAMILY_REVIEWER
    assert gh.calls_with("gh", "pr", "ready") == []
    assert gh.calls_with("gh", "api") == []


def test_reviewer_reporting_unavailable_stops_with_no_cross_family_reviewer() -> None:
    gh = FakeGh()
    outcome = _gate(_plan_at("route"), gh, FakeReviewer(NoCrossFamilyReviewerError("none")))
    assert outcome.reason is PrReadyReason.NO_CROSS_FAMILY_REVIEWER
    assert gh.calls_with("gh", "pr", "ready") == []


def test_unknown_level_is_treated_as_route_level() -> None:
    gh, reviewer = FakeGh(), FakeReviewer()
    assert _gate(_plan_at("unknown"), gh, reviewer).ready
    assert len(reviewer.requests) == 1


@pytest.mark.parametrize(
    "result",
    [
        ReviewResult("FAIL", "ocr", "gm/gemini-3", detail="bad"),
        ReviewResult("ERROR", "ocr", "", detail="timeout"),
        ReviewResult(
            "PASS",
            "ocr",
            "gm/gemini-3",
            findings=(ReviewFinding("high", "bug", "a.py", 1, "broken"),),
        ),
        ReviewResult("PASS", "ocr", "gm/gemini-3", findings_lost=1),
        RuntimeError("reviewer crashed"),
    ],
)
def test_failing_or_erroring_review_stops_with_review_failed(
    result: ReviewResult | Exception,
) -> None:
    gh = FakeGh()
    outcome = _gate(_plan_at("route"), gh, FakeReviewer(result))
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.REVIEW_FAILED
    assert gh.calls_with("gh", "pr", "ready") == []
    assert gh.calls_with("gh", "api") == []


@pytest.mark.parametrize("route_id", ["", "   "])
def test_passing_review_without_a_reviewer_route_stops_with_review_failed(route_id: str) -> None:
    gh = FakeGh()
    outcome = _gate(_plan_at("route"), gh, FakeReviewer(ReviewResult("PASS", "ocr", route_id)))
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.REVIEW_FAILED
    assert outcome.review_ran
    assert gh.calls_with("gh", "pr", "ready") == []


def test_reviewer_in_implementer_family_is_not_cross_family() -> None:
    gh = FakeGh()
    result = ReviewResult("PASS", "ocr", "cc/claude-opus-5")
    outcome = _gate(_plan_at("route"), gh, FakeReviewer(result))
    assert outcome.reason is PrReadyReason.NO_CROSS_FAMILY_REVIEWER
    assert gh.calls_with("gh", "pr", "ready") == []


def test_checks_are_queried_for_the_exact_head_sha_and_red_stops() -> None:
    gh = FakeGh(polls=[[_run("success"), _run("failure")]])
    outcome = _gate(_plan_at("family"), gh)
    assert outcome.reason is PrReadyReason.CI_RED
    api = gh.calls_with("gh", "api")
    assert api
    assert all(HEAD in call[2] for call in api)
    assert gh.calls_with("gh", "pr", "ready") == []


def test_stale_head_stops_before_checks() -> None:
    gh = FakeGh(head="f" * 40)
    outcome = _gate(_plan_at("family"), gh)
    assert outcome.reason is PrReadyReason.HEAD_MOVED
    assert gh.calls_with("gh", "api") == []
    assert gh.calls_with("gh", "pr", "ready") == []


def test_pending_checks_at_deadline_stop_with_ci_pending_without_real_waiting() -> None:
    gh = FakeGh(polls=[[_run(None, status="in_progress")]])
    t = FakeTime()
    outcome = _gate(_plan_at("family"), gh, time_=t, deadline_s=100.0, poll_interval_s=30.0)
    assert outcome.reason is PrReadyReason.CI_PENDING
    assert gh.calls_with("gh", "pr", "ready") == []
    assert t.now == pytest.approx(1100.0)
    assert sum(t.sleeps) == pytest.approx(100.0)
    assert outcome.polls == len(gh.calls_with("gh", "api")) // 2


def test_checks_that_turn_green_within_deadline_ready_the_pr_once() -> None:
    gh = FakeGh(
        polls=[[_run(None, status="queued")], [_run(None, status="in_progress")], [_run("success")]]
    )
    t = FakeTime()
    outcome = _gate(_plan_at("family"), gh, time_=t)
    assert outcome.ready
    assert outcome.polls == 3
    assert t.sleeps == [30.0, 30.0]
    ready = gh.calls_with("gh", "pr", "ready")
    assert ready == [["gh", "pr", "ready", PR]]


def test_no_checks_reported_yet_is_pending_not_green() -> None:
    gh = FakeGh(polls=[[]])
    outcome = _gate(_plan_at("family"), gh, deadline_s=60.0)
    assert outcome.reason is PrReadyReason.CI_PENDING
    assert gh.calls_with("gh", "pr", "ready") == []


def test_failed_gh_pr_ready_is_gh_failed() -> None:
    outcome = _gate(_plan_at("family"), FakeGh(ready_rc=1))
    assert outcome.reason is PrReadyReason.GH_FAILED


def test_invalid_request_runs_nothing() -> None:
    gh = FakeGh()
    outcome = run_pr_ready_gate(_plan_at("family"), pr_ref=" ", head_sha=HEAD, runner=gh)
    assert outcome.reason is PrReadyReason.INVALID_REQUEST
    assert gh.calls == []


@pytest.mark.parametrize("level", ["family", "route"])
def test_no_mutating_verbs_and_all_argv_are_lists(level: str) -> None:
    gh = FakeGh(polls=[[_run(None, status="queued")], [_run("success")]])
    _gate(_plan_at(level), gh, FakeReviewer())
    assert gh.calls
    for call in gh.calls:
        assert isinstance(call, list)
        assert FORBIDDEN_VERBS.isdisjoint(call[:3])
        assert call[0] in {"gh", "git"}
        if call[:2] == ["gh", "pr"]:
            assert call[2] in {"view", "ready"}


def test_default_runner_never_uses_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    class FakeProcess:
        """What ``subprocess.Popen`` gives ``run_command``: a finished process with output."""

        def __init__(self, argv: Any, **kwargs: Any) -> None:
            seen.append({"argv": argv, **kwargs})
            self.returncode = 0
            self._stdout = json.dumps({"total_count": 0})
            if argv[:3] == ["gh", "pr", "view"]:
                self._stdout = json.dumps({"headRefOid": HEAD})
            elif "/check-runs" in argv[-1]:
                self._stdout = json.dumps({"total_count": 1, "check_runs": [_run("success")]})

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return self._stdout, ""

    monkeypatch.setattr("verdict.orchestration.deliver_publish.subprocess.Popen", FakeProcess)
    outcome = run_pr_ready_gate(_plan_at("family"), pr_ref=PR, head_sha=HEAD)
    assert outcome.ready
    assert seen
    for call in seen:
        assert isinstance(call["argv"], list)
        assert not call.get("shell")


def test_source_has_no_merge_approve_close_or_label_call() -> None:
    source = Path(ready_gate.__file__).read_text(encoding="utf-8")
    start = source.index("# --- Pull-request ready gate")
    gate_source = source[start:]
    for verb in ("merge", "approve", "close", "label", "shell=True"):
        assert f'"{verb}"' not in gate_source
    assert "shell=True" not in gate_source


# --- F1: the deadline must be finite and positive --------------------------------------


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_non_finite_or_non_positive_deadline_is_invalid_request_and_runs_nothing(
    deadline: float,
) -> None:
    gh, reviewer = FakeGh(), FakeReviewer()
    outcome = _gate(_plan_at("route"), gh, reviewer, deadline_s=deadline)
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.INVALID_REQUEST
    assert gh.calls == []
    assert reviewer.requests == []


def test_default_deadline_is_sixty_minutes() -> None:
    assert ready_gate.DEFAULT_CI_DEADLINE_S == 3600.0


# --- F6: the head is re-read immediately before promotion ------------------------------


def test_head_that_moves_during_polling_stops_with_head_moved_and_never_readies() -> None:
    other = "f" * 40
    # first pr view (before polling) sees HEAD; the one right before gh pr ready sees a new commit
    gh = FakeGh(polls=[[_run(None, status="in_progress")], [_run("success")]], heads=[HEAD, other])
    outcome = _gate(_plan_at("family"), gh)
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.HEAD_MOVED
    assert other in outcome.message
    assert gh.calls_with("gh", "pr", "ready") == []
    assert len(gh.calls_with("gh", "pr", "view")) == 2
    # the promotion check is the last thing before gh pr ready would have run
    assert gh.calls[-1][:3] == ["gh", "pr", "view"]


def _gate_expecting(sha: str, gh: FakeGh) -> ready_gate.PrReadyOutcome:
    t = FakeTime()
    return run_pr_ready_gate(
        _plan_at("family"),
        pr_ref=PR,
        head_sha=sha,
        reviewer=None,
        runner=gh,
        deadline_s=120.0,
        poll_interval_s=30.0,
        clock=t.clock,
        sleep=t.sleep,
    )


def test_reported_head_that_is_a_prefix_of_the_expected_commit_stops_with_head_moved() -> None:
    gh = FakeGh(head=HEAD[:12])
    outcome = _gate_expecting(HEAD, gh)
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.HEAD_MOVED
    assert gh.calls_with("gh", "pr", "ready") == []


def test_a_different_full_head_stops_with_head_moved() -> None:
    gh = FakeGh(head="f" * 40)
    outcome = _gate_expecting(HEAD, gh)
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.HEAD_MOVED
    assert gh.calls_with("gh", "pr", "ready") == []


def test_abbreviated_expected_value_still_matches_the_full_head_it_prefixes() -> None:
    gh = FakeGh(head=HEAD)
    outcome = _gate_expecting(HEAD[:12], gh)
    assert outcome.ready
    assert len(gh.calls_with("gh", "pr", "ready")) == 1


def test_exact_forty_character_head_passes() -> None:
    gh = FakeGh(head=HEAD)
    outcome = _gate_expecting(HEAD, gh)
    assert outcome.ready
    assert len(gh.calls_with("gh", "pr", "ready")) == 1


def test_unmoved_head_is_read_twice_and_the_pr_is_readied_last() -> None:
    gh = FakeGh(polls=[[_run(None, status="queued")], [_run("success")]])
    outcome = _gate(_plan_at("family"), gh)
    assert outcome.ready
    kinds = [c[:3] for c in gh.calls if c[:3] in (["gh", "pr", "view"], ["gh", "pr", "ready"])]
    assert kinds == [["gh", "pr", "view"], ["gh", "pr", "view"], ["gh", "pr", "ready"]]
    assert gh.calls[-2][:3] == ["gh", "pr", "view"]


# --- F7: every page of check runs is evaluated -------------------------------------------


def test_a_red_check_on_a_later_page_stops_with_ci_red() -> None:
    runs = [_run("success") for _ in range(5)] + [_run("failure")]
    gh = FakeGh(polls=[runs], page_size=2)
    outcome = _gate(_plan_at("family"), gh)
    assert outcome.reason is PrReadyReason.CI_RED
    assert gh.calls_with("gh", "pr", "ready") == []
    pages = [c[2] for c in gh.calls if "/check-runs" in c[2]]
    assert len(pages) == 3
    assert all(HEAD in p for p in pages)


def test_a_pending_check_on_a_later_page_is_not_green() -> None:
    runs = [_run("success") for _ in range(4)] + [_run(None, status="in_progress")]
    gh = FakeGh(polls=[runs], page_size=2)
    outcome = _gate(_plan_at("family"), gh, deadline_s=60.0)
    assert outcome.reason is PrReadyReason.CI_PENDING
    assert gh.calls_with("gh", "pr", "ready") == []


def test_all_pages_green_readies_the_pr() -> None:
    gh = FakeGh(polls=[[_run("success") for _ in range(5)]], page_size=2)
    assert _gate(_plan_at("family"), gh).ready


def test_total_count_larger_than_what_was_read_fails_closed() -> None:
    """A server that reports more runs than it served must never read as green."""
    gh = FakeGh(polls=[[_run("success") for _ in range(3)]], total_count=7, page_size=100)
    outcome = _gate(_plan_at("family"), gh, deadline_s=60.0)
    assert not outcome.ready
    assert outcome.reason is PrReadyReason.CI_PENDING
    assert gh.calls_with("gh", "pr", "ready") == []


# --- F8: legacy commit statuses alone can pass -----------------------------------------


def test_status_only_repository_with_green_statuses_passes() -> None:
    gh = FakeGh(
        polls=[[]],
        statuses={"state": "success", "total_count": 2, "statuses": [{"state": "success"}] * 2},
    )
    outcome = _gate(_plan_at("family"), gh)
    assert outcome.ready
    assert len(gh.calls_with("gh", "pr", "ready")) == 1


def test_status_only_repository_with_a_red_status_is_ci_red() -> None:
    gh = FakeGh(
        polls=[[]],
        statuses={"state": "failure", "total_count": 1, "statuses": [{"state": "failure"}]},
    )
    assert _gate(_plan_at("family"), gh).reason is PrReadyReason.CI_RED


def test_status_only_repository_with_pending_statuses_stays_pending() -> None:
    gh = FakeGh(
        polls=[[]],
        statuses={"state": "pending", "total_count": 1, "statuses": [{"state": "pending"}]},
    )
    assert _gate(_plan_at("family"), gh, deadline_s=60.0).reason is PrReadyReason.CI_PENDING


def test_no_evidence_in_either_mechanism_stays_pending() -> None:
    gh = FakeGh(polls=[[]], statuses={"state": "pending", "total_count": 0, "statuses": []})
    outcome = _gate(_plan_at("family"), gh, deadline_s=60.0)
    assert outcome.reason is PrReadyReason.CI_PENDING
    assert gh.calls_with("gh", "pr", "ready") == []


def test_green_check_runs_with_pending_statuses_stay_pending() -> None:
    gh = FakeGh(
        polls=[[_run("success")]],
        statuses={"state": "pending", "total_count": 1, "statuses": [{"state": "pending"}]},
    )
    assert _gate(_plan_at("family"), gh, deadline_s=60.0).reason is PrReadyReason.CI_PENDING


# --- the extra review's summary rides on the outcome -------------------------------------

_SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


def test_passing_review_ends_ready_with_a_pass_summary() -> None:
    outcome = _gate(_plan_at("route"), FakeGh(), FakeReviewer())
    assert outcome.ready
    assert outcome.extra_review == ready_gate.ExtraReviewSummary(
        route_id="gm/gemini-3", status="PASS", findings=0, blocking=0, detail=""
    )


def test_failing_review_with_two_findings_one_blocking_is_summarised() -> None:
    result = ReviewResult(
        "FAIL",
        "ocr",
        "gm/gemini-3",
        findings=(
            ReviewFinding("high", "bug", "a.py", 1, "broken"),
            ReviewFinding("low", "style", "a.py", 2, "nit"),
        ),
        detail="two findings",
    )
    outcome = _gate(_plan_at("route"), FakeGh(), FakeReviewer(result))
    assert outcome.reason is PrReadyReason.REVIEW_FAILED
    assert outcome.extra_review == ready_gate.ExtraReviewSummary(
        route_id="gm/gemini-3", status="FAIL", findings=2, blocking=1, detail="two findings"
    )


def test_error_result_detail_is_redacted_and_capped() -> None:
    detail = f"key {_SECRET} " + "x" * 2000
    result = ReviewResult("ERROR", "ocr", "gm/gemini-3", detail=detail)
    outcome = _gate(_plan_at("route"), FakeGh(), FakeReviewer(result))
    summary = outcome.extra_review
    assert summary is not None
    assert summary.status == "ERROR"
    assert _SECRET not in summary.detail
    assert len(summary.detail) == ready_gate._MAX_DETAIL
    assert summary.detail == ready_gate.redact_text(detail)[: ready_gate._MAX_DETAIL]


def test_refused_result_still_carries_its_summary() -> None:
    result = ReviewResult("PASS", "ocr", "cc/claude-opus-5")
    outcome = _gate(_plan_at("route"), FakeGh(), FakeReviewer(result))
    assert outcome.reason is PrReadyReason.NO_CROSS_FAMILY_REVIEWER
    assert outcome.extra_review is not None
    assert outcome.extra_review.route_id == "cc/claude-opus-5"
    assert outcome.extra_review.status == "PASS"


def test_raising_reviewer_gives_an_error_summary_with_the_type_name() -> None:
    reviewer = FakeReviewer(RuntimeError(f"crashed with {_SECRET}"))
    outcome = _gate(_plan_at("route"), FakeGh(), reviewer)
    assert outcome.reason is PrReadyReason.REVIEW_FAILED
    summary = outcome.extra_review
    assert summary is not None
    assert summary.status == "ERROR"
    assert summary.route_id == ""
    assert (summary.findings, summary.blocking) == (0, 0)
    assert "RuntimeError" in summary.detail
    assert _SECRET not in summary.detail


@pytest.mark.parametrize("reviewer", [None, FakeReviewer(NoCrossFamilyReviewerError("none"))])
def test_no_reviewer_gives_no_summary(reviewer: FakeReviewer | None) -> None:
    outcome = _gate(_plan_at("route"), FakeGh(), reviewer)
    assert outcome.reason is PrReadyReason.NO_CROSS_FAMILY_REVIEWER
    assert outcome.extra_review is None


def test_family_level_run_has_no_summary() -> None:
    assert _gate(_plan_at("family"), FakeGh(), FakeReviewer()).extra_review is None


def test_outcome_before_the_review_step_has_no_summary() -> None:
    outcome = run_pr_ready_gate(_plan_at("route"), pr_ref=" ", head_sha=HEAD, runner=FakeGh())
    assert outcome.reason is PrReadyReason.INVALID_REQUEST
    assert outcome.extra_review is None


def test_summary_survives_a_red_check_a_moved_head_and_a_pending_check() -> None:
    red = _gate(_plan_at("route"), FakeGh(polls=[[_run("failure")]]), FakeReviewer())
    assert red.reason is PrReadyReason.CI_RED
    moved = _gate(_plan_at("route"), FakeGh(head="f" * 40), FakeReviewer())
    assert moved.reason is PrReadyReason.HEAD_MOVED
    pending = _gate(
        _plan_at("route"),
        FakeGh(polls=[[_run(None, status="in_progress")]]),
        FakeReviewer(),
        deadline_s=60.0,
    )
    assert pending.reason is PrReadyReason.CI_PENDING
    for outcome in (red, moved, pending):
        assert outcome.extra_review is not None
        assert outcome.extra_review.route_id == "gm/gemini-3"
        assert outcome.extra_review.status == "PASS"


def test_positional_outcome_construction_still_works() -> None:
    outcome = ready_gate.PrReadyOutcome(True, None, "ok", True, 2)
    assert outcome.extra_review is None
