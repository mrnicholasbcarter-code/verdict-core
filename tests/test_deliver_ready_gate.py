"""Tests for the pull-request ready gate (``run_pr_ready_gate``).

git, ``gh``, the cross-family reviewer, the clock and sleep are all fakes. Nothing
live runs and no real home directory is touched.
"""

from __future__ import annotations

import json
import subprocess
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
        ready_rc: int = 0,
    ) -> None:
        self.calls: list[list[str]] = []
        self.polls = [list(p) for p in polls]
        self.head = head
        self.ready_rc = ready_rc

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        call = list(argv)
        self.calls.append(call)
        if call[:3] == ["gh", "pr", "view"]:
            return CommandResult(0, json.dumps({"headRefOid": self.head}))
        if call[:2] == ["gh", "api"] and call[2].endswith("/status"):
            return CommandResult(0, json.dumps({"state": "success", "total_count": 0}))
        if call[:2] == ["gh", "api"] and "/check-runs" in call[2]:
            runs = self.polls[0] if len(self.polls) == 1 else self.polls.pop(0)
            return CommandResult(0, json.dumps({"check_runs": runs}))
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

    def spy(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": args[0], **kwargs})
        if args[0][:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args[0], 0, json.dumps({"headRefOid": HEAD}), "")
        if "/check-runs" in args[0][-1]:
            return subprocess.CompletedProcess(
                args[0], 0, json.dumps({"check_runs": [_run("success")]}), ""
            )
        return subprocess.CompletedProcess(args[0], 0, json.dumps({"total_count": 0}), "")

    monkeypatch.setattr("verdict.orchestration.deliver_publish.subprocess.run", spy)
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
