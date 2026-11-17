"""Tests for the PR watcher core and its concrete ``gh`` client.

The core is driven through injected fake clients.  The concrete client is
driven through GraphQL payload fixtures under tests/fixtures/pr_watcher, with
only its process call replaced.  The fixtures are synthetic: built by hand in
the shape of the client's queries, not captured from GitHub, so they show that
the client and the core agree with each other, not that GitHub agrees.  Each test names the failure it catches.  The
setup/doctor timer and the live smoke are outside this file.
"""

from __future__ import annotations

import collections
import json
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.pr_watcher import (
    _adapter_action,
    reconcile_merged_prs,
    request_bounded_rerun,
    run_watcher,
)
from verdict.pr_watcher_github import (
    CALL_TIMEOUT_SECONDS,
    CONTEXTS_PAGE_SIZE,
    MAX_PAGES,
    MERGED_PULLS_PAGE_SIZE,
    MERGED_PULLS_QUERY,
    OPEN_PULLS_PAGE_SIZE,
    OPEN_PULLS_QUERY,
    PULL_QUERY,
    GitHubClientError,
    WatcherGitHubClient,
    _GhApiClient,
    create_github_client,
    linear_issue_for,
)
from verdict.pr_watcher_github import assert_no_mutation as assert_github_no_mutation
from verdict.pr_watcher_notifications import (
    build_digest_event,
    build_linear_event,
    deliver_linear_events,
    transition_event_type,
)
from verdict.pr_watcher_state import (
    BlockRecord,
    LockAcquisitionError,
    MalformedStateError,
    RegressionRecord,
    RepairRecord,
    UnsafeKeyError,
    WatcherState,
    _safe_key,
)

# ============================================================================
# Fixtures
# ============================================================================


class FakeGitHubClient:
    """Injectable fake GitHub client for tests."""

    def __init__(self) -> None:
        self._open_prs: dict[str, list[dict[str, Any]]] = {}
        self._pr_details: dict[tuple[str, int], dict[str, Any]] = {}
        self._checks: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._merged_prs: dict[str, list[dict[str, Any]]] = {}
        self.rerun_calls: list[dict[str, Any]] = []
        self.merged_since_calls: list[str | None] = []
        self.fail_open: dict[str, Exception] = {}
        self.fail_merged: dict[str, Exception] = {}
        self.fail_pr: dict[tuple[str, int], Exception] = {}
        self._before_rerun: Callable[[], None] | None = None
        self.checks_calls: list[str] = []

    def _set_open_prs(self, repo: str, prs: list[dict[str, Any]]) -> None:
        self._open_prs[repo] = prs

    def _set_pr(self, repo: str, pr_number: int, details: dict[str, Any]) -> None:
        self._pr_details[(repo, pr_number)] = details

    def _set_checks(self, repo: str, sha: str, checks: list[dict[str, Any]]) -> None:
        self._checks[(repo, sha)] = checks

    def _set_reviews(self, repo: str, pr_number: int, reviews: list[dict[str, Any]]) -> None:
        self._reviews[(repo, pr_number)] = reviews

    def _set_merged_prs(self, repo: str, prs: list[dict[str, Any]]) -> None:
        self._merged_prs[repo] = prs

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        if repo in self.fail_open:
            raise self.fail_open[repo]
        return list(self._open_prs.get(repo, []))

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        key = (repo, pr_number)
        if key in self.fail_pr:
            raise self.fail_pr[key]
        if key not in self._pr_details:
            raise KeyError(f"no PR details for {repo}/{pr_number}")
        details = dict(self._pr_details[key])
        head = details["head_sha"]
        self.checks_calls.append(head)
        return {"commit_oid": head, "checks": list(self._checks.get((repo, head), [])), **details}

    def list_merged_prs(self, repo: str, since: str | None) -> list[dict[str, Any]]:
        self.merged_since_calls.append(since)
        if repo in self.fail_merged:
            raise self.fail_merged[repo]
        return [
            {"reviews": list(self._reviews.get((repo, p["number"]), [])), **p}
            for p in self._merged_prs.get(repo, [])
            if since is None or p["merged_at"] >= since
        ]

    def rerun_check(self, job_id: Any, repo: str) -> dict[str, Any]:
        if self._before_rerun is not None:
            self._before_rerun()
        self.rerun_calls.append({"job_id": job_id, "repo": repo})
        return {"status": "queued"}


class FakeLinearClient:
    """Injectable fake Linear client for tests."""

    def __init__(self) -> None:
        self.comments: list[dict[str, Any]] = []
        self.attempts: list[str] = []
        self._before_comment: Callable[[], None] | None = None
        self._fail_on: set[str] = set()

    def _set_fail(self, *issue_ids: str) -> None:
        self._fail_on.update(issue_ids)

    def _clear_fail(self) -> None:
        self._fail_on.clear()

    def add_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        self.attempts.append(issue_id)
        if self._before_comment is not None:
            self._before_comment()
        if issue_id in self._fail_on:
            raise RuntimeError("simulated Linear API failure")
        self.comments.append({"issue_id": issue_id, "body": body})
        return {"id": "comment-1", "status": "created"}


@pytest.fixture
def gh() -> FakeGitHubClient:
    return FakeGitHubClient()


@pytest.fixture
def linear() -> FakeLinearClient:
    return FakeLinearClient()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "verdict-prime"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def ws(state_dir: Path) -> WatcherState:
    return WatcherState(state_dir)


REPO = "org/repo"
T0 = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
RUN_INTERVAL = timedelta(minutes=5)
MERGED_AT = "2026-03-02T11:58:00Z"

# Check names exactly as GitHub reports them for this repository's workflows.
REAL_CHECK_NAMES = (
    "test (3.11)",
    "Analyze (python)",
    "osv-security / osv-scan",
    "install-smoke (3.10)",
    "lint",
    "proof-full",
)
# classify_ci calls a failure INFRA only when the check name contains an infra
# pattern, and no real check name here does. This single synthetic name exists
# to reach that branch.
INFRA_NAMED_CHECK = "runner timeout"


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def check(
    name: str, conclusion: str | None = "SUCCESS", status: str = "COMPLETED", **extra: Any
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion, "required": True, **extra}


def open_pr(
    gh: FakeGitHubClient,
    number: int,
    head: str,
    checks: list[dict[str, Any]],
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    repo: str = REPO,
    **extra: Any,
) -> None:
    """Register one open PR at ``head`` with its exact-head checks."""
    pr = {
        "number": number,
        "head_sha": head,
        "mergeable": mergeable,
        "merge_state_status": merge_state,
        **extra,
    }
    gh._set_open_prs(repo, [*gh._open_prs.get(repo, []), pr])
    gh._set_pr(repo, number, pr)
    gh._set_checks(repo, head, checks)


def outcomes(result: dict[str, Any]) -> list[tuple[int, str, str]]:
    return [(o["pr_number"], o["classification"], o["action"]) for o in result["observations"]]


def disk(ws: WatcherState) -> dict[str, Any]:
    """The state as persisted, read without going through the watcher."""
    loaded: dict[str, Any] = json.loads(ws.state_file.read_text())
    return loaded


# ============================================================================
# Requirement 1: Exact-head polling classifies every open configured PR
# ============================================================================


class TestExactHeadPolling:
    """Requirement: Exact-head polling classifies every open configured pull request."""

    def test_every_open_pr_is_classified_from_the_checks_of_its_own_head(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches checks being read for the wrong head, or a listed PR being skipped.

        The fake returns one list: pagination is the client's job and is not
        exercised here, and nor is a head that moves between the listing and
        the checks read.
        """
        open_pr(gh, 1, "a" * 40, [check("test (3.11)")])
        open_pr(gh, 2, "b" * 40, [check("Analyze (python)", None, "IN_PROGRESS")])
        open_pr(gh, 3, "c" * 40, [check("lint", "FAILURE")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert gh.checks_calls == ["a" * 40, "b" * 40, "c" * 40]
        assert outcomes(result) == [
            (1, "GREEN", "READY"),
            (2, "PENDING", "NO_ACTION"),
            (3, "CODE_FAILURE", "REPAIR"),
        ]

    def test_zero_checks_is_not_green_and_gets_a_repair_record_with_a_reason(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a PR with no checks at all being treated as ready."""
        open_pr(gh, 1, "a" * 40, [])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "EMPTY", "REPAIR")]
        repairs = disk(ws)["records"]["repair"]
        assert [(r["classification"], r["reason"]) for r in repairs] == [
            ("EMPTY", "PR classified as EMPTY")
        ]

    @pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS"])
    def test_unfinished_check_stays_pending_with_no_action(
        self, gh: FakeGitHubClient, ws: WatcherState, status: str
    ) -> None:
        """Catches a still-running check being rerun, repaired or reported."""
        open_pr(gh, 1, "a" * 40, [check("proof-full", None, status, job_id="job-1")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "PENDING", "NO_ACTION")]
        assert gh.rerun_calls == []
        assert disk(ws)["records"] == {"repair": [], "block": [], "regression": []}

    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [(29, "PENDING"), (30, "PENDING"), (31, "STALLED"), (95, "STALLED")],
    )
    def test_check_is_stalled_only_after_more_than_thirty_minutes(
        self, gh: FakeGitHubClient, ws: WatcherState, minutes: int, expected: str
    ) -> None:
        """Catches an off-by-one at the 30 minute boundary in either direction."""
        started = iso(T0 - timedelta(minutes=minutes))
        open_pr(gh, 1, "a" * 40, [check("proof-full", None, "IN_PROGRESS", started_at=started)])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert result["observations"][0]["classification"] == expected

    def test_check_without_a_start_time_is_pending_not_stalled(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a missing start time being read as an old one."""
        open_pr(gh, 1, "a" * 40, [check("proof-full", None, "IN_PROGRESS")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "PENDING", "NO_ACTION")]

    @pytest.mark.parametrize(
        ("checks", "classification"),
        [
            ([check("osv-security / osv-scan", "CANCELLED", job_id=None)], "CANCELLED"),
            ([check("osv-security / osv-scan", "TIMED_OUT", job_id=None)], "INFRA_FAILURE"),
            (
                [
                    check("lint", "CANCELLED", job_id=7),
                    check("osv-security / osv-scan", "CANCELLED", job_id=None),
                ],
                "CANCELLED",
            ),
        ],
        ids=["cancelled", "timed out", "one of two"],
    )
    def test_check_without_an_actions_job_is_repaired_not_rerun(
        self,
        gh: FakeGitHubClient,
        ws: WatcherState,
        checks: list[dict[str, Any]],
        classification: str,
    ) -> None:
        """Catches a rerun request for a check that is not a GitHub Actions job."""
        open_pr(gh, 1, "a" * 40, checks)

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(2)]

        assert [outcomes(r) for r in runs] == [[(1, classification, "REPAIR")]] * 2
        assert gh.rerun_calls == []
        assert disk(ws)["rerun_attempts"] == {}
        repairs = disk(ws)["records"]["repair"]
        assert [r["reason"] for r in repairs] == [
            "check 'osv-security / osv-scan' is not a GitHub Actions job and cannot be rerun"
        ]
        assert disk(ws)["records"]["block"] == []

    def test_rerun_budget_follows_the_workflow_run_not_the_job_attempt(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an unbounded rerun loop when a rerun gives the job a new id.

        The budget key uses the workflow run id, which a rerun keeps; the job id
        is only where the rerun request is sent.
        """
        head = "a" * 40
        open_pr(gh, 1, head, [check("lint", "CANCELLED", job_id=101, workflow_run_id=55)])
        first = run_watcher(gh, ws, [REPO], now=T0)

        gh._set_checks(REPO, head, [check("lint", "CANCELLED", job_id=102, workflow_run_id=55)])
        later = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in (1, 2)]

        assert outcomes(first) == [(1, "CANCELLED", "RETRY")]
        assert [outcomes(r) for r in later] == [[(1, "CANCELLED", "BLOCKED")]] * 2
        assert gh.rerun_calls == [{"job_id": 101, "repo": REPO}]
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr1:{head}:lint:55": 1}

    def test_stalled_rerun_targets_the_stalled_check_not_a_finished_one(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the rerun going to whichever check happens to be listed first."""
        started = iso(T0 - timedelta(minutes=95))
        open_pr(
            gh,
            1,
            "a" * 40,
            [
                check("lint", job_id="job-done"),
                check("proof-full", None, "IN_PROGRESS", started_at=started, job_id="job-slow"),
            ],
        )

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert gh.rerun_calls == []
        assert [r["observations"][0]["action"] for r in runs] == ["REPAIR"] * 3
        assert runs[0]["records"][0]["reason"].startswith("stalled:")
        # Assert the stalled check name is in the repair record
        assert runs[0]["records"][0]["failing_checks"] == ["proof-full"]
        assert runs[0]["records"][0]["failing_check_details"][0]["name"] == "proof-full"
        assert runs[0]["records"][0]["failing_check_details"][0]["job_id"] == "job-slow"

    def test_job_id_reaches_github_exactly_as_the_client_reported_it(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the numeric id GitHub reports being rewritten on its way to the rerun."""
        head = "a" * 40
        open_pr(gh, 1, head, [check("lint", "CANCELLED", job_id=987654321)])

        run_watcher(gh, ws, [REPO], now=T0)

        assert gh.rerun_calls == [{"job_id": 987654321, "repo": REPO}]
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr1:{head}:lint:987654321": 1}

    def test_head_that_moved_before_its_checks_were_read_is_reported_and_not_acted_on(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches checks of a newer commit being recorded against the head that was listed."""
        old_head, new_head = "b" * 40, "c" * 40
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")])
        open_pr(gh, 2, old_head, [check("lint")])
        gh._set_pr(REPO, 2, {"number": 2, "head_sha": new_head})
        gh._set_checks(REPO, new_head, [check("proof-full", "CANCELLED", job_id="job-2")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "CODE_FAILURE", "REPAIR"), (2, "HEAD_CHANGED", "NO_ACTION")]
        assert result["observations"][1]["head"] == old_head
        assert [r["pr_number"] for r in result["records"]] == [1]
        assert [e["pr_number"] for e in result["notifications"]] == [1]
        assert gh.rerun_calls == []
        assert [k for k in disk(ws)["pr_states"] if ":pr2:" in k] == []

    def test_checks_belonging_to_another_commit_than_the_head_are_not_acted_on(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a rollup for an older commit being classified as the head's result."""
        head = "b" * 40
        open_pr(gh, 3, head, [check("lint", "FAILURE")])
        gh._set_pr(REPO, 3, {"number": 3, "head_sha": head, "commit_oid": "9" * 40})

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(3, "HEAD_CHANGED", "NO_ACTION")]
        assert result["records"] == []
        assert result["notifications"] == []
        assert disk(ws)["pr_states"] == {}

    def test_head_that_moved_while_the_latest_commit_still_reads_as_the_old_one_is_not_acted_on(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the head comparison trusting the checks' commit alone."""
        listed = "b" * 40
        open_pr(gh, 4, listed, [check("lint", "FAILURE")])
        gh._set_pr(REPO, 4, {"number": 4, "head_sha": "c" * 40, "commit_oid": listed})
        gh._set_checks(REPO, "c" * 40, [check("lint", "FAILURE")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(4, "HEAD_CHANGED", "NO_ACTION")]
        assert result["records"] == []

    def test_optional_hung_check_does_not_make_the_pr_stalled(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a check nobody requires being rerun because it has run for a long time."""
        started = iso(T0 - timedelta(minutes=95))
        hung = check("Analyze (python)", None, "IN_PROGRESS", started_at=started, job_id="job-9")
        open_pr(gh, 1, "a" * 40, [check("lint"), {**hung, "required": False}])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "GREEN", "READY")]
        assert gh.rerun_calls == []

    def test_only_the_required_failed_check_is_rerun_when_an_optional_one_failed_too(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an optional check being rerun alongside the required one that warranted it."""
        optional = {**check("Analyze (python)", "CANCELLED", job_id="job-9"), "required": False}
        open_pr(gh, 1, "a" * 40, [optional, check("lint", "CANCELLED", job_id="job-1")])

        run_watcher(gh, ws, [REPO], now=T0)

        assert gh.rerun_calls == [{"job_id": "job-1", "repo": REPO}]
        assert list(disk(ws)["rerun_attempts"]) == [f"{REPO}:pr1:{'a' * 40}:lint:job-1"]

    def test_optional_failed_check_never_decides_the_result(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a check branch protection does not require causing a repair or a rerun."""
        optional_failure = {**check("Analyze (python)", "FAILURE"), "required": False}
        optional_cancel = {**check("osv-security / osv-scan", "CANCELLED", job_id="job-9")}
        optional_cancel["required"] = False
        open_pr(gh, 1, "a" * 40, [check("lint"), optional_failure, optional_cancel])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "GREEN", "READY")]
        assert result["records"] == []
        assert gh.rerun_calls == []

    def test_no_required_check_is_missing_required_and_reported_once_per_head(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an unprotected repository reading as green, or being reported every run."""
        optional = {**check("lint"), "required": False}
        open_pr(gh, 1, "a" * 40, [optional])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert [outcomes(r) for r in runs] == [[(1, "MISSING_REQUIRED", "REPAIR")]] * 3
        assert [[x["type"] for x in r["records"]] for r in runs] == [["repair"], [], []]
        assert [kinds(r) for r in runs] == [["new_failure"], [], []]
        repairs = disk(ws)["records"]["repair"]
        assert [(r["classification"], r["reason"]) for r in repairs] == [
            ("MISSING_REQUIRED", "PR classified as MISSING_REQUIRED")
        ]

    @pytest.mark.parametrize("conclusion", ["TIMED_OUT", "STARTUP_FAILURE"])
    def test_timed_out_or_startup_failure_is_rerun_once_then_blocked(
        self, gh: FakeGitHubClient, ws: WatcherState, conclusion: str
    ) -> None:
        """Catches an infrastructure conclusion sitting at PENDING with no action."""
        open_pr(gh, 1, "a" * 40, [check("test (3.11)", conclusion, job_id="job-1")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert [outcomes(r) for r in runs] == [
            [(1, "INFRA_FAILURE", "RETRY")],
            [(1, "INFRA_FAILURE", "BLOCKED")],
            [(1, "INFRA_FAILURE", "BLOCKED")],
        ]
        assert gh.rerun_calls == [{"job_id": "job-1", "repo": REPO}]
        assert len(disk(ws)["records"]["block"]) == 1

    @pytest.mark.parametrize("conclusion", ["ACTION_REQUIRED", "STALE"])
    def test_action_required_or_stale_gets_one_repair_record_naming_the_conclusion(
        self, gh: FakeGitHubClient, ws: WatcherState, conclusion: str
    ) -> None:
        """Catches these conclusions being waited on, or the record hiding why it was written."""
        open_pr(gh, 1, "a" * 40, [check("lint", conclusion, job_id="job-1"), check("proof-full")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(2)]

        assert [outcomes(r) for r in runs] == [[(1, "CODE_FAILURE", "REPAIR")]] * 2
        assert gh.rerun_calls == []
        repairs = disk(ws)["records"]["repair"]
        assert [(r["failing_checks"], r["reason"]) for r in repairs] == [
            (["lint"], f"required check concluded {conclusion}")
        ]

    def test_naive_clock_is_rejected_before_anything_is_read_or_written(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a naive datetime silently shifting the stall and cursor arithmetic."""
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")])

        with pytest.raises(ValueError, match="timezone-aware"):
            run_watcher(gh, ws, [REPO], now=datetime(2026, 3, 2, 12, 0, 0))

        assert gh.checks_calls == []
        assert not ws.state_file.exists()


# ============================================================================
# Requirement 2: Merged PR discovery reconciles the open poll
# ============================================================================


class TestMergedPRReconciliation:
    """Requirement: Merged PR discovery reconciles the open poll."""

    def merged(self, gh: FakeGitHubClient, number: int, head: str, *states: str) -> None:
        gh._set_merged_prs(REPO, [{"number": number, "head_sha": head, "merged_at": MERGED_AT}])
        gh._set_reviews(REPO, number, [{"state": s, "id": i} for i, s in enumerate(states)])

    def test_changes_requested_review_is_a_regression(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a merged PR with a fail review being passed over."""
        self.merged(gh, 42, "e" * 40, "CHANGES_REQUESTED")

        regs = reconcile_merged_prs(gh, ws, REPO, now=T0)

        assert [(r["record"]["pr_number"], r["record"]["head"]) for r in regs] == [(42, "e" * 40)]
        assert regs[0]["review_sources"] == ["github_changes_requested"]

    def reviewed(self, gh: FakeGitHubClient, head: str, *reviews: tuple[str, str, int]) -> None:
        """Merged PR 42 at ``head`` with (state, commit, minute submitted) reviews."""
        gh._set_merged_prs(REPO, [{"number": 42, "head_sha": head, "merged_at": MERGED_AT}])
        gh._set_reviews(
            REPO,
            42,
            [
                {"state": s, "commit_oid": c, "submitted_at": f"2026-03-02T10:{m:02d}:00Z"}
                for s, c, m in reviews
            ],
        )

    def test_changes_requested_on_an_older_commit_is_cleared_by_a_later_approval_of_the_head(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a review that was addressed and then approved being reported as a fail."""
        old, head = "0" * 40, "e" * 40
        self.reviewed(gh, head, ("CHANGES_REQUESTED", old, 1), ("APPROVED", head, 30))

        assert reconcile_merged_prs(gh, ws, REPO, now=T0) == []

    @pytest.mark.parametrize(
        "later",
        [
            [],
            [("APPROVED", "0" * 40, 30)],
            [("COMMENTED", "e" * 40, 30)],
            [("DISMISSED", "e" * 40, 30)],
        ],
        ids=["nothing later", "approval of the old commit", "comment on head", "dismissed on head"],
    )
    def test_changes_requested_on_an_older_commit_stands_without_a_later_approval_of_the_head(
        self, gh: FakeGitHubClient, ws: WatcherState, later: list[tuple[str, str, int]]
    ) -> None:
        """Catches an outstanding change request being dropped because the head moved on."""
        self.reviewed(gh, "e" * 40, ("CHANGES_REQUESTED", "0" * 40, 1), *later)

        regs = reconcile_merged_prs(gh, ws, REPO, now=T0)

        assert [r["review_sources"] for r in regs] == [["github_changes_requested"]]

    def test_approval_of_the_head_before_the_change_request_does_not_clear_it(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches 'later' being ignored, so that any approval of the head clears a request."""
        head = "e" * 40
        self.reviewed(gh, head, ("APPROVED", head, 1), ("CHANGES_REQUESTED", "0" * 40, 30))

        regs = reconcile_merged_prs(gh, ws, REPO, now=T0)

        assert [r["review_sources"] for r in regs] == [["github_changes_requested"]]

    def test_changes_requested_on_the_merged_head_counts_even_after_an_approval_of_it(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a change request made on the merged commit itself being cleared.

        This is the one case where the two readings of the rule differ; it follows
        "counts if on the merged head, or if no later approval of that head exists".
        """
        head = "e" * 40
        self.reviewed(gh, head, ("CHANGES_REQUESTED", head, 1), ("APPROVED", head, 30))

        regs = reconcile_merged_prs(gh, ws, REPO, now=T0)

        assert [r["review_sources"] for r in regs] == [["github_changes_requested"]]

    def test_change_request_on_a_commit_github_no_longer_has_is_treated_as_an_older_commit(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a force-pushed-away review commit being read as the merged head."""
        head = "e" * 40
        gh._set_merged_prs(REPO, [{"number": 42, "head_sha": head, "merged_at": MERGED_AT}])
        request = {
            "state": "CHANGES_REQUESTED",
            "commit_oid": None,
            "submitted_at": "2026-03-02T10:01:00Z",
        }
        approval = {"state": "APPROVED", "commit_oid": head, "submitted_at": "2026-03-02T10:30:00Z"}

        gh._set_reviews(REPO, 42, [request, approval])
        cleared = reconcile_merged_prs(gh, ws, REPO, now=T0)
        gh._set_reviews(REPO, 42, [request])
        standing = reconcile_merged_prs(gh, ws, REPO, now=T0)

        assert cleared == []
        assert [r["review_sources"] for r in standing] == [["github_changes_requested"]]

    def test_fail_receipt_counts_only_for_the_merged_head(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a receipt for another PR or an older head being applied to this merge."""
        head = "e" * 40
        self.merged(gh, 42, head)
        other_head = {f"{REPO}:42:{'0' * 40}": {"status": "FAIL"}}
        other_pr = {f"{REPO}:43:{head}": {"status": "FAIL"}}
        passed = {f"{REPO}:42:{head}": {"status": "PASS"}}
        exact = {f"{REPO}:42:{head}": {"status": "FAIL"}}

        assert reconcile_merged_prs(gh, ws, REPO, other_head, now=T0) == []
        assert reconcile_merged_prs(gh, ws, REPO, other_pr, now=T0) == []
        assert reconcile_merged_prs(gh, ws, REPO, passed, now=T0) == []
        regs = reconcile_merged_prs(gh, ws, REPO, exact, now=T0)
        assert [r["review_sources"] for r in regs] == [["verdict_ocr_fail"]]

    def test_approved_merge_with_no_fail_evidence_is_not_a_regression(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches every merged PR being recorded as a regression."""
        self.merged(gh, 60, "e" * 40, "APPROVED")

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert result["records"] == []
        assert result["notifications"] == []
        assert disk(ws)["records"]["regression"] == []


class TestMergedCursor:
    """The merged-PR cursor is durable, advances, and bounds every query."""

    def test_first_run_uses_bounded_initial_lookback(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an unbounded first query: no cursor must not mean since=None."""
        run_watcher(gh, ws, [REPO], now=T0)
        assert gh.merged_since_calls == [iso(T0 - timedelta(hours=24))]

    def test_cursor_is_persisted_and_advances_across_runs(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a cursor that is computed but never saved."""
        run_watcher(gh, ws, [REPO], now=T0)
        assert disk(ws)["merged_cursors"] == {REPO: iso(T0)}
        assert disk(ws)["last_run_at"] == iso(T0)

        run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)
        assert disk(ws)["merged_cursors"] == {REPO: iso(T0 + RUN_INTERVAL)}
        assert disk(ws)["last_run_at"] == iso(T0 + RUN_INTERVAL)

    def test_second_run_queries_since_cursor_minus_lookback(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a lookback that is skipped or applied to the wrong instant."""
        run_watcher(gh, ws, [REPO], now=T0)
        run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)
        assert gh.merged_since_calls[1] == iso(T0 - timedelta(minutes=5))

    def test_cursor_ahead_of_clock_falls_back_to_bounded_lookback(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a clock step backwards leaving a future cursor that hides merges."""
        run_watcher(gh, ws, [REPO], now=T0)
        earlier = T0 - timedelta(hours=1)
        run_watcher(gh, ws, [REPO], now=earlier)
        assert gh.merged_since_calls[1] == iso(earlier - timedelta(hours=24))
        assert disk(ws)["merged_cursors"] == {REPO: iso(earlier)}

    def test_failed_reconciliation_does_not_advance_that_repositorys_cursor(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a cursor that advances past merges the run never managed to list."""
        run_watcher(gh, ws, [REPO], now=T0)

        gh.fail_merged[REPO] = GitHubClientError("search unavailable")
        result = run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        assert result["repo_errors"] == [
            {"repo": REPO, "error": "search unavailable", "kind": "github_error"}
        ]
        # Cursor should not advance past the failed reconciliation (stays at T0)
        assert disk(ws)["merged_cursors"] == {REPO: iso(T0)}

    def test_open_on_run_n_merged_with_fail_review_before_next_run(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches both a missed merge and a regression duplicated by the lookback."""
        head = "5" * 40
        open_pr(gh, 42, head, [check("test (3.11)")])
        run_watcher(gh, ws, [REPO], now=T0)
        assert disk(ws)["records"]["regression"] == []

        gh._set_open_prs(REPO, [])
        merged_at = iso(T0 + timedelta(minutes=2))
        gh._set_merged_prs(REPO, [{"number": 42, "head_sha": head, "merged_at": merged_at}])
        gh._set_reviews(REPO, 42, [{"state": "CHANGES_REQUESTED", "id": 7}])

        new_records = []
        for run in (1, 2, 3):
            result = run_watcher(gh, ws, [REPO], now=T0 + run * RUN_INTERVAL)
            new_records.append([r["type"] for r in result["records"]])

        assert new_records == [["regression"], [], []]
        regressions = disk(ws)["records"]["regression"]
        assert [(r["pr_number"], r["head"]) for r in regressions] == [(42, head)]
        assert None not in gh.merged_since_calls
        # The lookback really did re-list the merged PR on the run after discovery.
        assert merged_at >= gh.merged_since_calls[2]

    def test_failed_open_prs_then_merged_regression_found_via_24h_lookback(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Run 1 fails list_open_prs; run 2 queries merged since T0-24h and finds regression."""
        # Run 1: list_open_prs fails for REPO
        gh.fail_open[REPO] = GitHubClientError("HTTP 502 from GitHub")
        result1 = run_watcher(gh, ws, [REPO], now=T0)
        assert result1["ok"] is False
        assert result1["repo_errors"][0]["kind"] == "github_error"
        # No cursor advanced for REPO
        assert disk(ws)["merged_cursors"] == {}

        # A PR was open at T0-4h, merged at T0-4h with CHANGES_REQUESTED
        # (simulated by setting up merged PRs and reviews for the query at run 2)
        head = "9" * 40
        merged_at = iso(T0 - timedelta(hours=4))
        # Clear the error for run 2
        gh.fail_open.clear()
        # Run 2 will query merged PRs since T0 - 24h (no cursor)
        gh._set_open_prs(REPO, [])
        gh._set_merged_prs(REPO, [{"number": 99, "head_sha": head, "merged_at": merged_at}])
        gh._set_reviews(REPO, 99, [{"state": "CHANGES_REQUESTED", "id": 42}])

        result2 = run_watcher(gh, ws, [REPO], now=T0 + timedelta(minutes=5))
        assert result2["ok"] is True
        assert result2["repo_errors"] == []
        # Regression should be recorded
        regressions = [r for r in result2["records"] if r["type"] == "regression"]
        assert len(regressions) == 1
        assert regressions[0]["pr_number"] == 99
        assert regressions[0]["head"] == head
        # The merged_since call should use T0 - 24h (since no cursor was set)
        # Run 1 didn't call list_merged_prs, so this is the first call (index 0)
        assert gh.merged_since_calls[0] == iso(T0 + timedelta(minutes=5) - timedelta(hours=24))
        # Cursor should now be advanced to run 2's time
        assert disk(ws)["merged_cursors"] == {REPO: iso(T0 + timedelta(minutes=5))}


# ============================================================================
# Requirement 3: Stable rerun identity and fencing
# ============================================================================


class TestStableRerunFencing:
    """Requirement: Stable rerun identity and fencing."""

    def fenced(self, gh: FakeGitHubClient, *checks: dict[str, Any], head: str = "a" * 40) -> None:
        """What the immediate pre-rerun reread will see for PR 42."""
        gh._set_pr(REPO, 42, {"number": 42, "head_sha": head})
        gh._set_checks(REPO, head, list(checks))

    def test_changed_head_aborts_without_a_rerun_or_a_spent_attempt(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a rerun against a head that moved, or the old key being advanced."""
        self.fenced(gh, check("lint", "CANCELLED", job_id="job-1"), head="b" * 40)

        result = request_bounded_rerun(gh, ws, REPO, 42, "a" * 40, "lint", "job-1", max_attempts=3)

        assert (result["rerun"], result["type"]) == (False, "fence_mismatch")
        assert gh.rerun_calls == []
        assert not ws.state_file.exists()

    @pytest.mark.parametrize(
        "reread",
        [
            [],
            [check("lint", "CANCELLED", job_id="job-2")],
            [check("proof-full", "CANCELLED", job_id="job-1")],
            [check("lint", "SUCCESS", job_id="job-1")],
            [check("lint", None, "IN_PROGRESS", job_id="job-1")],
            [{**check("lint", "CANCELLED", job_id="job-1"), "required": False}],
        ],
        ids=[
            "check gone",
            "new job id",
            "other name",
            "now passing",
            "now running",
            "not required",
        ],
    )
    def test_changed_check_identity_aborts_without_a_rerun_or_a_spent_attempt(
        self, gh: FakeGitHubClient, ws: WatcherState, reread: list[dict[str, Any]]
    ) -> None:
        """Catches a rerun of a check that was replaced, restarted or fixed since it was read."""
        self.fenced(gh, *reread)

        result = request_bounded_rerun(gh, ws, REPO, 42, "a" * 40, "lint", "job-1", max_attempts=3)

        assert (result["rerun"], result["type"]) == (False, "fence_mismatch")
        assert gh.rerun_calls == []
        assert not ws.state_file.exists()

    def test_checks_reread_for_another_commit_abort_without_a_rerun_or_a_spent_attempt(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the fence accepting a same-named check that belongs to a different commit."""
        head = "a" * 40
        self.fenced(gh, check("lint", "CANCELLED", job_id="job-1"))
        gh._set_pr(REPO, 42, {"number": 42, "head_sha": head, "commit_oid": "9" * 40})

        result = request_bounded_rerun(gh, ws, REPO, 42, head, "lint", "job-1", max_attempts=3)

        assert (result["rerun"], result["type"]) == (False, "fence_mismatch")
        assert gh.rerun_calls == []
        assert not ws.state_file.exists()

    def test_check_that_recovers_between_the_poll_and_the_rerun_is_not_rerun(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the run-level path skipping the check-identity fence."""
        head = "a" * 40
        open_pr(gh, 1, head, [check("lint", "CANCELLED", job_id="job-1")])
        reads = 0
        original = gh.get_pr

        def get_pr(repo: str, pr_number: int) -> dict[str, Any]:
            nonlocal reads
            reads += 1
            if reads == 2:
                gh._set_checks(REPO, head, [check("lint", "SUCCESS", job_id="job-1")])
            return original(repo, pr_number)

        gh.get_pr = get_pr

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert [r["type"] for r in result["reruns"]] == ["fence_mismatch"]
        assert gh.rerun_calls == []
        assert disk(ws)["rerun_attempts"] == {}

    def test_attempt_is_on_disk_before_github_is_called(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the counter being written after, not before, the rerun request."""
        head = "a" * 40
        self.fenced(gh, check("Analyze (python)", "CANCELLED", job_id="job-1"))
        seen: list[dict[str, int]] = []
        gh._before_rerun = lambda: seen.append(disk(ws)["rerun_attempts"])

        result = request_bounded_rerun(gh, ws, REPO, 42, head, "Analyze (python)", "job-1")

        assert result["rerun"] is True
        assert seen == [{f"{REPO}:pr42:{head}:Analyze (python):job-1": 1}]

    def test_budget_of_three_allows_exactly_three_reruns(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the bound being off by one, or not being enforced at all."""
        head = "a" * 40
        self.fenced(gh, check("lint", "CANCELLED", job_id="job-1"))

        results = [
            request_bounded_rerun(gh, ws, REPO, 42, head, "lint", "job-1", max_attempts=3)
            for _ in range(5)
        ]

        assert [r["rerun"] for r in results] == [True, True, True, False, False]
        assert [r.get("type") for r in results[3:]] == ["exhausted", "exhausted"]
        assert len(gh.rerun_calls) == 3
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr42:{head}:lint:job-1": 3}

    def test_key_separates_check_name_job_pr_and_head(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches two different reruns sharing one budget."""
        head = "a" * 40
        self.fenced(
            gh,
            check("lint", "CANCELLED", job_id="job-1"),
            check("lint", "CANCELLED", job_id="job-2"),
            check("test (3.11)", "CANCELLED", job_id="job-1"),
        )
        gh._set_pr(REPO, 43, {"number": 43, "head_sha": "b" * 40})
        gh._set_checks(REPO, "b" * 40, [check("lint", "CANCELLED", job_id="job-1")])

        request_bounded_rerun(gh, ws, REPO, 42, head, "lint", "job-1")
        request_bounded_rerun(gh, ws, REPO, 42, head, "lint", "job-2")
        request_bounded_rerun(gh, ws, REPO, 42, head, "test (3.11)", "job-1")
        request_bounded_rerun(gh, ws, REPO, 43, "b" * 40, "lint", "job-1")

        assert disk(ws)["rerun_attempts"] == {
            f"{REPO}:pr42:{head}:lint:job-1": 1,
            f"{REPO}:pr42:{head}:lint:job-2": 1,
            f"{REPO}:pr42:{head}:test (3.11):job-1": 1,
            f"{REPO}:pr43:{'b' * 40}:lint:job-1": 1,
        }
        assert len(gh.rerun_calls) == 4

    def test_each_failed_check_gets_its_own_single_rerun(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches one check's spent budget denying another check its only rerun."""
        head = "a" * 40
        open_pr(
            gh,
            1,
            head,
            [
                check("lint", "CANCELLED", job_id="job-1"),
                check("proof-full", "CANCELLED", job_id="job-2"),
            ],
        )

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert [c["job_id"] for c in gh.rerun_calls] == ["job-1", "job-2"]
        assert [r["observations"][0]["action"] for r in runs] == ["RETRY", "BLOCKED", "BLOCKED"]
        assert disk(ws)["rerun_attempts"] == {
            f"{REPO}:pr1:{head}:lint:job-1": 1,
            f"{REPO}:pr1:{head}:proof-full:job-2": 1,
        }

    def test_new_head_gets_a_fresh_budget(self, gh: FakeGitHubClient, ws: WatcherState) -> None:
        """Catches a budget keyed without the head, which would block every later push."""
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")])
        run_watcher(gh, ws, [REPO], now=T0)
        run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        gh._set_open_prs(REPO, [])
        open_pr(gh, 1, "b" * 40, [check("lint", "CANCELLED", job_id="job-2")])
        result = run_watcher(gh, ws, [REPO], now=T0 + 2 * RUN_INTERVAL)

        assert outcomes(result) == [(1, "CANCELLED", "RETRY")]
        assert [c["job_id"] for c in gh.rerun_calls] == ["job-1", "job-2"]


# ============================================================================
# Requirement 4: Bounded recovery adapter is normative
# ============================================================================


class TestBoundedRecoveryAdapter:
    """Requirement: Bounded recovery adapter is normative."""

    @pytest.mark.parametrize(
        ("classification", "attempts", "expected"),
        [
            ("GREEN", 0, "READY"),
            ("PROCEED_MERGE", 0, "READY"),
            ("PENDING", 0, "NO_ACTION"),
            ("WAIT_CI", 0, "NO_ACTION"),
            ("INFRA_FAILURE", 0, "RETRY"),
            ("CANCELLED", 0, "RETRY"),
            ("STALLED", 0, "REPAIR"),
            ("RETRY_CI", 0, "RETRY"),
            ("CODE_FAILURE", 0, "REPAIR"),
            ("CORRECTIVE_IMPLEMENTATION", 0, "REPAIR"),
            ("CONFLICT", 0, "REPAIR"),
            ("BEHIND", 0, "REPAIR"),
            ("REBASE_AND_REPROOF", 0, "REPAIR"),
            ("EMPTY", 0, "REPAIR"),
            ("MISSING_REQUIRED", 0, "REPAIR"),
            ("INFRA_FAILURE", 1, "BLOCKED"),
            ("CANCELLED", 1, "BLOCKED"),
            ("STALLED", 1, "REPAIR"),
            ("RETRY_CI", 2, "BLOCKED"),
            ("BLOCKED", 0, "BLOCKED"),
            # A spent budget never turns a repair or a ready PR into a block.
            ("CODE_FAILURE", 5, "REPAIR"),
            ("GREEN", 5, "READY"),
        ],
    )
    def test_adapter_table_row(self, classification: str, attempts: int, expected: str) -> None:
        """Catches any row of the normative adapter table mapping to the wrong result."""
        assert _adapter_action(classification, attempts, 1) == expected

    def test_adapter_follows_ci_recovery_action(self) -> None:
        """The watcher adapter's output matches the shared CI recovery policy.

        For classifications where the shared policy uses the SAME attempt budget
        semantics (GREEN, PENDING, INFRA_FAILURE, CANCELLED, CONFLICT), the
        watcher's _adapter_action must map the recovery action to the watcher
        action space:
        PROCEED_MERGE -> READY, WAIT_CI -> NO_ACTION, RETRY_CI -> RETRY/BLOCKED,
        REBASE_AND_REPROOF -> REPAIR, BLOCKED -> BLOCKED.

        CODE_FAILURE is excluded: ci_recovery_action treats its 'attempts' as
        corrective-implementation attempts, while the watcher treats them as
        rerun attempts. The watcher's adapter table says CODE_FAILURE is always
        REPAIR, never BLOCKED, regardless of attempt count.
        """
        from verdict.ci_policy import ci_recovery_action

        # Classifications covered by ci_recovery_action with MATCHING budget semantics
        covered = {"GREEN", "PENDING", "INFRA_FAILURE", "CANCELLED", "CONFLICT"}
        for classification in covered:
            for attempts in (0, 1, 5):
                max_attempts = 1 if attempts == 0 else 3
                recovery = ci_recovery_action(classification, attempts, max_attempts)
                action = recovery["action"]
                # Map ci_recovery_action's action to watcher's action space
                if action == "PROCEED_MERGE":
                    expected = "READY"
                elif action == "WAIT_CI":
                    expected = "NO_ACTION"
                elif action == "RETRY_CI":
                    expected = "RETRY" if attempts < max_attempts else "BLOCKED"
                elif action == "REBASE_AND_REPROOF":
                    expected = "REPAIR"
                elif action == "BLOCKED":
                    expected = "BLOCKED"
                else:
                    raise AssertionError(f"Unknown recovery action: {action}")
                actual = _adapter_action(classification, attempts, max_attempts)
                assert actual == expected, (
                    f"classification={classification} attempts={attempts} max_attempts={max_attempts}: "
                    f"ci_recovery_action gives {action} -> expected {expected}, "
                    f"but _adapter_action returned {actual}"
                )

        # CODE_FAILURE is explicitly NOT delegated to ci_recovery_action because
        # its attempt budget differs. The watcher's adapter table says it is
        # always REPAIR, even with a spent rerun budget.
        assert _adapter_action("CODE_FAILURE", 5, 1) == "REPAIR"

    def test_spent_budget_reports_blocked_and_requests_nothing(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a run that still reports RETRY after its rerun budget is spent."""
        head = "2" * 40
        open_pr(gh, 7, head, [check("install-smoke (3.10)", "CANCELLED", job_id="job-9")])

        first = run_watcher(gh, ws, [REPO], now=T0)
        assert first["observations"][0]["action"] == "RETRY"
        assert gh.rerun_calls == [{"job_id": "job-9", "repo": REPO}]

        later = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in (1, 2, 3)]

        assert [r["observations"][0]["action"] for r in later] == ["BLOCKED"] * 3
        assert [r["reruns"] for r in later] == [[], [], []]
        assert len(gh.rerun_calls) == 1
        assert [[rec["type"] for rec in r["records"]] for r in later] == [["block"], [], []]
        blocks = disk(ws)["records"]["block"]
        assert [(b["pr_number"], b["head"], b["reason"]) for b in blocks] == [
            (7, head, "attempts exhausted for CANCELLED")
        ]
        assert disk(ws)["pr_states"][f"state:{REPO}:pr7:{head}"]["action"] == "BLOCKED"

    @pytest.mark.parametrize(
        ("mergeable", "merge_state"),
        [
            ("UNKNOWN", "UNKNOWN"),
            ("MERGEABLE", "UNKNOWN"),
            ("MERGEABLE", "BLOCKED"),
            ("MERGEABLE", "DRAFT"),
            ("UNKNOWN", "UNSTABLE"),
        ],
    )
    def test_green_checks_without_clean_mergeability_are_pending_not_ready(
        self, gh: FakeGitHubClient, ws: WatcherState, mergeable: str, merge_state: str
    ) -> None:
        """Catches a PR being called READY while GitHub has not said it can merge."""
        open_pr(gh, 1, "a" * 40, [check("lint")], mergeable, merge_state)

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(2)]

        assert [outcomes(r) for r in runs] == [[(1, "PENDING", "NO_ACTION")]] * 2
        assert runs[0]["observations"][0]["ci_classification"] == "GREEN"
        assert [r["records"] + r["notifications"] for r in runs] == [[], []]
        assert gh.rerun_calls == []

    @pytest.mark.parametrize("merge_state", ["UNSTABLE", "HAS_HOOKS"])
    def test_green_required_checks_on_a_mergeable_pr_are_ready_despite_optional_failures(
        self, gh: FakeGitHubClient, ws: WatcherState, merge_state: str
    ) -> None:
        """Catches a failing optional check holding a mergeable PR at PENDING for ever.

        GitHub reports such a PR as MERGEABLE with merge state UNSTABLE, not CLEAN.
        """
        optional = {**check("Analyze (python)", "FAILURE"), "required": False}
        open_pr(gh, 1, "a" * 40, [check("lint"), optional], "MERGEABLE", merge_state)

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, "GREEN", "READY")]
        assert result["records"] == []

    def test_green_pr_is_ready_and_nothing_is_written_to_github_or_linear(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches any side effect on a green, mergeable PR."""
        open_pr(gh, 1, "a" * 40, [check("test (3.11)"), check("lint")], linear_issue="ENG-1")

        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert outcomes(result) == [(1, "GREEN", "READY")]
        assert gh.rerun_calls == []
        assert linear.attempts == []
        assert result["records"] == []
        assert result["notifications"] == []
        assert disk(ws)["records"] == {"repair": [], "block": [], "regression": []}


class TestRealCheckNames:
    """Every real check name survives classification, keying and persistence."""

    @pytest.mark.parametrize("name", REAL_CHECK_NAMES)
    def test_success_is_ready_with_no_side_effect(
        self, gh: FakeGitHubClient, ws: WatcherState, name: str
    ) -> None:
        """Catches a real name being misread as anything but a passing check."""
        open_pr(gh, 3, "3" * 40, [check(name, "SUCCESS", job_id="job-3")])
        result = run_watcher(gh, ws, [REPO], now=T0)

        assert [(o["classification"], o["action"]) for o in result["observations"]] == [
            ("GREEN", "READY")
        ]
        assert gh.rerun_calls == []
        assert disk(ws)["records"] == {"repair": [], "block": [], "regression": []}
        assert disk(ws)["rerun_attempts"] == {}

    @pytest.mark.parametrize("name", REAL_CHECK_NAMES)
    def test_failure_writes_one_repair_record_naming_the_check(
        self, gh: FakeGitHubClient, ws: WatcherState, name: str
    ) -> None:
        """Catches a real name being dropped from, or mangled in, the repair record."""
        head = "4" * 40
        open_pr(gh, 4, head, [check(name, "FAILURE", job_id="job-4")])
        result = run_watcher(gh, ws, [REPO], now=T0)

        assert [(o["classification"], o["action"]) for o in result["observations"]] == [
            ("CODE_FAILURE", "REPAIR")
        ]
        assert gh.rerun_calls == []
        repairs = disk(ws)["records"]["repair"]
        assert [(r["pr_number"], r["head"], r["failing_checks"]) for r in repairs] == [
            (4, head, [name])
        ]

    @pytest.mark.parametrize("name", REAL_CHECK_NAMES)
    def test_cancelled_is_rerun_once_under_a_key_holding_the_exact_name(
        self, gh: FakeGitHubClient, ws: WatcherState, name: str
    ) -> None:
        """Catches key validation rejecting spaces, parentheses or slashes in a name."""
        head = "6" * 40
        open_pr(gh, 6, head, [check(name, "CANCELLED", job_id="job-6")])
        result = run_watcher(gh, ws, [REPO], now=T0)

        assert [(o["classification"], o["action"]) for o in result["observations"]] == [
            ("CANCELLED", "RETRY")
        ]
        assert gh.rerun_calls == [{"job_id": "job-6", "repo": REPO}]
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr6:{head}:{name}:job-6": 1}


# ============================================================================
# Requirement 5: Watcher capability excludes mutations
# ============================================================================


# The check is an allow-list: every public callable outside the protocol is
# refused, so these include names no prefix rule would have caught.
GITHUB_MUTATIONS = (
    "merge",
    "merge_pull_request",
    "push",
    "create_branch",
    "delete_branch",
    "edit_source",
    "force_push",
    "checkout",
    "commit",
    "apply_patch",
    "write_file",
    "create_pull_request",
    "delete_repository",
    "rerun_workflow",
    "dispatch_workflow",
    "approve",
    "delete_ref",
    "create_ref",
    "create_commit_status",
    "create_issue_comment",
    "enable_auto_merge",
    "cancel_workflow_run",
    "set_labels",
    "fail_job",
    "before_merge",
)
LINEAR_MUTATIONS = (
    "create_issue",
    "update_issue",
    "delete_issue",
    "create_branch",
    "push",
    "merge",
    "archive_issue",
    "assign_issue",
    "set_state",
    "issue_update",
    "remove_comment",
)


class TestCapabilityRestriction:
    """Requirement: Watcher capability excludes mutations."""

    def test_client_protocol_is_exactly_reads_and_one_bounded_rerun(self) -> None:
        """Catches a mutation method being added to the interface the watcher is given."""
        public = {name for name in vars(WatcherGitHubClient) if not name.startswith("_")}
        assert public == {"list_open_prs", "get_pr", "list_merged_prs", "rerun_check"}

    @pytest.mark.parametrize("method", GITHUB_MUTATIONS)
    def test_github_client_with_a_mutation_method_is_refused(
        self, ws: WatcherState, method: str
    ) -> None:
        """Catches the capability check being skipped, or a forbidden name going unlisted."""
        client_type = type("MutatingClient", (FakeGitHubClient,), {method: lambda self: None})
        gh = client_type()
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert result["type"] == "client_refused"
        assert method in result["error"]
        assert gh.rerun_calls == []
        assert gh.checks_calls == []
        assert not ws.state_file.exists()

    @pytest.mark.parametrize("method", LINEAR_MUTATIONS)
    def test_linear_client_with_a_mutation_method_is_refused(
        self, gh: FakeGitHubClient, ws: WatcherState, method: str
    ) -> None:
        """Catches a Linear client that can do more than comment being accepted."""
        client_type = type("MutatingLinear", (FakeLinearClient,), {method: lambda self: None})
        linear = client_type()
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")], linear_issue="ENG-1")

        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert result["type"] == "client_refused"
        assert method in result["error"]
        assert linear.attempts == []
        assert not ws.state_file.exists()


# ============================================================================
# Requirement 6: Single writer and idempotent events
# ============================================================================


class TestSingleWriterIdempotency:
    """Requirement: Single writer and idempotent events."""

    def test_duplicate_timer_invocation_persists_one_rerun_and_one_event_per_key(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches the same exact-head invocation doing its work twice."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")], linear_issue="ENG-1")
        open_pr(gh, 2, "b" * 40, [check("proof-full", "CANCELLED", job_id="job-2")])

        first = run_watcher(gh, ws, [REPO], linear=linear, now=T0)
        second = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert [r["type"] for r in first["records"]] == ["repair"]
        assert [r["type"] for r in second["records"]] == ["block"]
        assert len(gh.rerun_calls) == 1
        assert linear.attempts == ["ENG-1"]
        state = disk(ws)
        assert len(state["records"]["repair"]) == 1
        assert len(state["records"]["block"]) == 1
        assert sorted(e["type"] for e in state["digest_events"].values()) == [
            "blocked",
            "new_failure",
            "new_failure",
        ]

    def test_overlapping_invocation_exits_without_state_or_provider_writes(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches a second watcher on the same directory running alongside the first."""
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")], linear_issue="ENG-1")
        holder = WatcherState(ws.state_dir)
        holder.acquire_lock()
        try:
            result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)
        finally:
            holder.release_lock()

        assert result["type"] == "lock_failure"
        assert "already holds the lock" in result["error"]
        assert gh.checks_calls == []
        assert gh.rerun_calls == []
        assert linear.attempts == []
        assert not ws.state_file.exists()

        after = run_watcher(gh, ws, [REPO], linear=linear, now=T0)
        assert len(gh.rerun_calls) == 1
        assert outcomes(after) == [(1, "CANCELLED", "RETRY")]

    def test_second_state_object_cannot_take_a_held_lock(self, ws: WatcherState) -> None:
        """Catches the lock being advisory in name only, or blocking instead of failing."""
        other = WatcherState(ws.state_dir)
        ws.acquire_lock()
        try:
            with pytest.raises(LockAcquisitionError, match="already holds the lock"):
                other.acquire_lock()
        finally:
            ws.release_lock()
        other.acquire_lock()
        other.release_lock()

    def test_lock_is_released_when_a_run_ends_in_an_error(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a failed run leaving the lock held and stopping every later run."""
        ws.state_file.write_text("{not json")
        assert run_watcher(gh, ws, [REPO], now=T0)["type"] == "malformed_state"

        other = WatcherState(ws.state_dir)
        other.acquire_lock()
        other.release_lock()


OTHER = "org/other"


class TestRepositoryIsolation:
    """A GitHub read error in one repository is reported and does not stop the others."""

    @pytest.mark.parametrize("stage", ["listing open PRs", "reading a PR", "listing merged PRs"])
    def test_failing_repository_is_reported_and_writes_nothing_while_the_other_is_handled(
        self, gh: FakeGitHubClient, ws: WatcherState, stage: str
    ) -> None:
        """Catches one unreachable repository stopping the run, or leaving half its work behind."""
        error = GitHubClientError("HTTP 502 from GitHub")
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")], repo=OTHER)
        open_pr(gh, 2, "b" * 40, [check("lint", "FAILURE")], repo=OTHER)
        open_pr(gh, 7, "c" * 40, [check("lint", "FAILURE")])
        if stage == "listing open PRs":
            gh.fail_open[OTHER] = error
        elif stage == "reading a PR":
            gh.fail_pr[(OTHER, 2)] = error
        else:
            gh.fail_merged[OTHER] = error

        result = run_watcher(gh, ws, [OTHER, REPO], now=T0)

        assert result["ok"] is False
        assert result["repo_errors"] == [
            {"repo": OTHER, "error": "HTTP 502 from GitHub", "kind": "github_error"}
        ]
        assert outcomes(result) == [(7, "CODE_FAILURE", "REPAIR")]
        assert [(r["repo"], r["pr_number"]) for r in result["records"]] == [(REPO, 7)]
        assert [e["repo"] for e in result["notifications"]] == [REPO]
        state = disk(ws)
        assert [r["repo"] for r in state["records"]["repair"]] == [REPO]
        assert list(state["pr_states"]) == [f"state:{REPO}:pr7:{'c' * 40}"]
        assert [e["repo"] for e in state["digest_events"].values()] == [REPO]
        # Failed repo's cursor is not advanced; only the healthy repo's cursor moves.
        assert state["merged_cursors"] == {REPO: iso(T0)}

    def test_repository_that_recovers_is_handled_in_full_on_the_next_run(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches work for a failed repository being lost instead of deferred."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")], repo=OTHER)
        gh.fail_merged[OTHER] = GitHubClientError("HTTP 502 from GitHub")
        run_watcher(gh, ws, [OTHER, REPO], now=T0)

        gh.fail_merged.clear()
        result = run_watcher(gh, ws, [OTHER, REPO], now=T0 + RUN_INTERVAL)

        assert result["ok"] is True
        assert result["repo_errors"] == []
        assert [(r["repo"], r["pr_number"]) for r in result["records"]] == [(OTHER, 1)]
        assert kinds(result) == ["new_failure"]
        assert disk(ws)["merged_cursors"] == {
            REPO: iso(T0 + RUN_INTERVAL),
            OTHER: iso(T0 + RUN_INTERVAL),
        }
        # The repository that failed had no cursor, so it starts from the bounded window.
        assert gh.merged_since_calls[2] == iso(T0 + RUN_INTERVAL - timedelta(hours=24))

    def test_read_error_at_the_rerun_fence_spends_no_attempt_and_fails_the_run(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a rerun going ahead unfenced, or an attempt being spent on a failed read."""
        head = "a" * 40
        open_pr(gh, 1, head, [check("lint", "CANCELLED", job_id="job-1")])
        reads = 0
        original = gh.get_pr

        def get_pr(repo: str, pr_number: int) -> dict[str, Any]:
            nonlocal reads
            reads += 1
            if reads == 2:
                raise GitHubClientError("HTTP 502 from GitHub")
            return original(repo, pr_number)

        gh.get_pr = get_pr

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert result["ok"] is False
        assert result["repo_errors"] == [
            {"repo": REPO, "error": "HTTP 502 from GitHub", "kind": "github_error"}
        ]
        assert [r["type"] for r in result["reruns"]] == ["read_error"]
        assert gh.rerun_calls == []
        assert disk(ws)["rerun_attempts"] == {}

    def test_failed_rerun_request_fails_the_run_and_still_spends_the_attempt(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a rejected rerun being reported as a healthy run, or being retried for ever."""
        head = "a" * 40
        open_pr(gh, 1, head, [check("lint", "CANCELLED", job_id="job-1")])

        def reject() -> None:
            raise GitHubClientError("HTTP 403 from GitHub")

        gh._before_rerun = reject
        first = run_watcher(gh, ws, [REPO], now=T0)
        gh._before_rerun = None
        second = run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        assert first["ok"] is False
        assert first["repo_errors"] == [
            {"repo": REPO, "error": "HTTP 403 from GitHub", "kind": "github_error"}
        ]
        assert [r["type"] for r in first["reruns"]] == ["api_error"]
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr1:{head}:lint:job-1": 1}
        assert (second["ok"], outcomes(second)) == (True, [(1, "CANCELLED", "BLOCKED")])
        assert gh.rerun_calls == []

    def test_an_error_that_is_not_a_github_read_error_is_not_swallowed(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a programming error being reported as an unreachable repository."""
        gh.fail_open[REPO] = RuntimeError("bug in the client")

        with pytest.raises(RuntimeError, match="bug in the client"):
            run_watcher(gh, ws, [REPO], now=T0)

        assert not ws.state_file.exists()

    def test_all_repos_down_with_github_error_saves_state_and_types_github_error(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an all-repos-down run skipping persistence, or moving a failed repo's cursor."""
        earlier = T0 - RUN_INTERVAL
        seeded = ws.set_merged_cursor(ws.load(), REPO, earlier)
        ws.save(ws.set_merged_cursor(seeded, OTHER, earlier))
        error = GitHubClientError("HTTP 502 from GitHub")
        gh.fail_open[REPO] = error
        gh.fail_open[OTHER] = error

        result = run_watcher(gh, ws, [REPO, OTHER], now=T0)

        assert result["ok"] is False
        assert result["type"] == "github_error"
        assert [e["repo"] for e in result["repo_errors"]] == [REPO, OTHER]
        assert all(e["kind"] == "github_error" for e in result["repo_errors"])
        state = disk(ws)
        assert state["last_run_at"] == iso(T0)
        # Neither failed repository's cursor moves, so the next run covers the same ground.
        assert state["merged_cursors"] == {REPO: iso(earlier), OTHER: iso(earlier)}

    def test_healthy_run_reports_ok(self, gh: FakeGitHubClient, ws: WatcherState) -> None:
        """Catches the success flag being absent or false on a run with nothing wrong."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert (result["ok"], result["repo_errors"]) == (True, [])


# ============================================================================
# Requirement 7: Code and merge failures route to durable repair
# ============================================================================


class TestCodeAndMergeFailures:
    """Requirement: Code and merge failures route to durable repair."""

    @pytest.mark.parametrize(
        ("mergeable", "merge_state", "classification"),
        [("CONFLICTING", "DIRTY", "CONFLICT"), ("MERGEABLE", "BEHIND", "BEHIND")],
    )
    def test_conflict_or_behind_writes_a_repair_record_and_mutates_nothing(
        self,
        gh: FakeGitHubClient,
        ws: WatcherState,
        mergeable: str,
        merge_state: str,
        classification: str,
    ) -> None:
        """Catches a merge-state failure being hidden behind passing checks."""
        head = "c" * 40
        open_pr(gh, 1, head, [check("test (3.11)", job_id="job-1")], mergeable, merge_state)

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(1, classification, "REPAIR")]
        assert gh.rerun_calls == []
        assert disk(ws)["records"]["repair"] == [
            {
                "type": "repair",
                "repo": REPO,
                "pr_number": 1,
                "head": head,
                "failing_checks": [],
                "failing_check_details": [],
                "classification": classification,
                "reason": f"PR classified as {classification}",
                "idempotency_key": f"repair:{REPO}:pr1:{head}:{classification}",
                "created_at": iso(T0),
            }
        ]

    def test_repair_record_names_each_failing_check_with_its_job_and_details_url(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a repair record that leaves the worker unable to find the failing job's log."""
        url = "https://github.com/org/repo/actions/runs/11/job/22"
        open_pr(
            gh,
            1,
            "c" * 40,
            [
                check("lint", "FAILURE", job_id=22, details_url=url),
                check("proof-full", "ACTION_REQUIRED", job_id=None, details_url=None),
                check("test (3.11)"),
                {**check("Analyze (python)", "FAILURE", job_id=33), "required": False},
            ],
        )

        run_watcher(gh, ws, [REPO], now=T0)

        record = disk(ws)["records"]["repair"][0]
        assert record["failing_checks"] == ["lint", "proof-full"]
        assert record["failing_check_details"] == [
            {"name": "lint", "conclusion": "FAILURE", "job_id": 22, "details_url": url},
            {
                "name": "proof-full",
                "conclusion": "ACTION_REQUIRED",
                "job_id": None,
                "details_url": None,
            },
        ]
        assert "log_excerpt" not in record

    def test_later_different_failure_on_the_same_head_gets_its_own_repair_record(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the first record for a head hiding what actually went wrong on it later.

        A PR polled before its checks register is EMPTY; the lint failure that
        follows on the same head must not be suppressed as a duplicate of that.
        """
        head = "c" * 40
        open_pr(gh, 1, head, [])
        first = run_watcher(gh, ws, [REPO], now=T0)

        gh._set_checks(REPO, head, [check("lint", "FAILURE", job_id=22)])
        later = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in (1, 2)]

        assert [(r["classification"], r["failing_checks"]) for r in first["records"]] == [
            ("EMPTY", [])
        ]
        assert [
            [(r["classification"], r["failing_checks"]) for r in run["records"]] for run in later
        ] == [[("CODE_FAILURE", ["lint"])], []]
        assert [r["idempotency_key"] for r in disk(ws)["records"]["repair"]] == [
            f"repair:{REPO}:pr1:{head}:EMPTY",
            f"repair:{REPO}:pr1:{head}:CODE_FAILURE",
        ]

    def test_code_failure_writes_one_repair_record_across_three_runs(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a repair record being rewritten or duplicated by every poll."""
        open_pr(gh, 1, "c" * 40, [check("lint", "FAILURE"), check("proof-full")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert [[r["type"] for r in run["records"]] for run in runs] == [["repair"], [], []]
        repairs = disk(ws)["records"]["repair"]
        assert [(r["failing_checks"], r["created_at"]) for r in repairs] == [(["lint"], iso(T0))]


# ============================================================================
# Requirement 8: Notifications occur only on transitions
# ============================================================================


def digest_lines(ws: WatcherState) -> list[dict[str, Any]]:
    """The digest file as written: one JSON object per line."""
    if not ws.digest_file.exists():
        return []
    return [json.loads(line) for line in ws.digest_file.read_text().splitlines()]


class TestDigestFile:
    """Digest events are appended to pr_watcher_digest.jsonl in the run that finds them."""

    def test_each_transition_is_one_line_and_repeated_runs_add_nothing(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the digest staying in memory only, or a line per poll instead of per transition."""
        open_pr(gh, 2, "b" * 40, [check("proof-full", "CANCELLED", job_id="job-2")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(4)]

        assert ws.digest_file.name == "pr_watcher_digest.jsonl"
        assert ws.digest_file.parent == ws.state_dir
        assert digest_lines(ws) == [runs[0]["notifications"][0], runs[1]["notifications"][0]]
        assert [line["type"] for line in digest_lines(ws)] == ["new_failure", "blocked"]
        assert disk(ws)["digest_unwritten"] == []

    def test_healthy_run_creates_no_digest_file(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an idle run touching the digest file."""
        open_pr(gh, 1, "a" * 40, [check("lint")])

        run_watcher(gh, ws, [REPO], now=T0)
        run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        assert not ws.digest_file.exists()

    def test_event_is_in_the_durable_state_before_its_line_is_appended(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a digest line for an event the state does not know, which a rerun would repeat."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")])
        seen: list[dict[str, Any]] = []
        append = ws.append_digest

        def recording(events: list[dict[str, Any]]) -> None:
            seen.append(disk(ws))
            append(events)

        ws.append_digest = recording
        result = run_watcher(gh, ws, [REPO], now=T0)

        key = result["notifications"][0]["idempotency_key"]
        assert [list(s["digest_events"]) for s in seen] == [[key]]
        assert seen[0]["digest_unwritten"] == [key]

    def test_death_before_the_line_is_appended_does_not_lose_the_event(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an event recorded in state and never written to the digest file."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")])
        append = ws.append_digest

        def die(events: list[dict[str, Any]]) -> None:
            raise Killed

        ws.append_digest = die
        with pytest.raises(Killed):
            run_watcher(gh, ws, [REPO], now=T0)
        assert digest_lines(ws) == []

        ws.append_digest = append
        later = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in (1, 2)]

        assert [kinds(r) for r in later] == [[], []]
        assert [(e["type"], e["pr_number"]) for e in digest_lines(ws)] == [("new_failure", 1)]

    def test_death_after_the_line_is_appended_does_not_write_it_twice(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the next run repeating a line whose append it could not confirm."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")])
        append = ws.append_digest

        def append_then_die(events: list[dict[str, Any]]) -> None:
            append(events)
            raise Killed

        ws.append_digest = append_then_die
        with pytest.raises(Killed):
            run_watcher(gh, ws, [REPO], now=T0)
        assert len(disk(ws)["digest_unwritten"]) == 1

        ws.append_digest = append
        run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        assert [(e["type"], e["pr_number"]) for e in digest_lines(ws)] == [("new_failure", 1)]
        assert disk(ws)["digest_unwritten"] == []

    def test_torn_last_line_is_kept_apart_from_the_next_event(self, ws: WatcherState) -> None:
        """Catches a new event being glued onto a half-written line and lost to every reader."""
        ws.digest_file.write_text('{"idempotency_key": "digest:old"}\n{"idempotency_key": "dig')
        event = {"idempotency_key": "digest:new", "type": "new_failure"}

        ws.append_digest([event])

        assert ws.digest_file.read_text().splitlines()[-1] == json.dumps(event, sort_keys=True)
        assert len(ws.digest_file.read_text().splitlines()) == 3

    def test_failed_digest_write_reports_the_error_and_causes_no_side_effect(
        self,
        gh: FakeGitHubClient,
        ws: WatcherState,
        linear: FakeLinearClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Catches a rerun or a Linear comment going out when the digest could not be written."""
        open_pr(
            gh,
            1,
            "a" * 40,
            [check("proof-full", "CANCELLED", job_id="job-1")],
            linear_issue="ENG-1",
        )
        ws.digest_file.mkdir()

        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert (result["ok"], result["type"]) == (False, "state_write_failed")
        assert "pr_watcher_digest.jsonl" in result["error"]
        assert gh.rerun_calls == []
        assert linear.attempts == []

        ws.digest_file.rmdir()
        retry = run_watcher(gh, ws, [REPO], linear=linear, now=T0 + RUN_INTERVAL)

        assert retry["ok"] is True
        assert [e["type"] for e in digest_lines(ws)] == ["new_failure"]
        assert gh.rerun_calls == [{"job_id": "job-1", "repo": REPO}]
        assert linear.attempts == ["ENG-1"]


def kinds(result: dict[str, Any]) -> list[str]:
    return [event["type"] for event in result["notifications"]]


class TestTransitionNotifications:
    """Requirement: Notifications occur only on transitions."""

    def test_new_failure_emits_one_digest_event_without_a_linear_client(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the digest event being tied to the Linear client being present."""
        head = "f" * 40
        open_pr(gh, 1, head, [check("lint", "FAILURE", job_id="job-1")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert [kinds(r) for r in runs] == [["new_failure"], [], []]
        event = runs[0]["notifications"][0]
        assert (event["repo"], event["pr_number"], event["head"]) == (REPO, 1, head)
        assert event["classification"] == "CODE_FAILURE"
        assert list(disk(ws)["digest_events"]) == [event["idempotency_key"]]

    def test_blocked_transition_emits_its_own_digest_event_once(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches the RETRY -> BLOCKED transition passing silently."""
        open_pr(gh, 2, "b" * 40, [check("proof-full", "CANCELLED", job_id="job-2")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(4)]

        assert [kinds(r) for r in runs] == [["new_failure"], ["blocked"], [], []]
        assert runs[1]["notifications"][0]["classification"] == "BLOCKED"

    def test_regression_digest_is_emitted_once_while_the_lookback_relists_it(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a regression notification repeated on every run that re-lists the PR."""
        gh._set_merged_prs(REPO, [{"number": 9, "head_sha": "e" * 40, "merged_at": MERGED_AT}])
        gh._set_reviews(REPO, 9, [{"state": "CHANGES_REQUESTED", "id": 1}])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * timedelta(minutes=1)) for n in range(3)]

        assert len(gh.merged_since_calls) == 3
        assert [kinds(r) for r in runs] == [["regression"], [], []]

    def test_new_head_with_the_same_failure_is_a_new_transition(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches idempotency keyed without the head, which would hide a re-broken PR."""
        open_pr(gh, 3, "1" * 40, [check("lint", "FAILURE")])
        first = run_watcher(gh, ws, [REPO], now=T0)

        gh._set_open_prs(REPO, [])
        open_pr(gh, 3, "2" * 40, [check("lint", "FAILURE")])
        second = run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        assert [e["head"] for e in first["notifications"]] == ["1" * 40]
        assert [e["head"] for e in second["notifications"]] == ["2" * 40]

    @pytest.mark.parametrize(
        "checks",
        [[check("test (3.11)")], [check("test (3.11)", None, "IN_PROGRESS")]],
        ids=["ready", "pending"],
    )
    def test_healthy_or_pending_pr_never_notifies(
        self,
        gh: FakeGitHubClient,
        ws: WatcherState,
        linear: FakeLinearClient,
        checks: list[dict[str, Any]],
    ) -> None:
        """Catches a message for a PR that is ready or simply still running."""
        open_pr(gh, 4, "a" * 40, checks, linear_issue="ENG-101")

        runs = [
            run_watcher(gh, ws, [REPO], linear=linear, now=T0 + n * RUN_INTERVAL) for n in range(2)
        ]

        assert [r["notifications"] for r in runs] == [[], []]
        assert linear.comments == []
        assert disk(ws)["digest_events"] == {}

    def test_linked_story_gets_exactly_one_comment_on_the_linked_issue(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches the comment going to something other than the linked story, or repeating."""
        open_pr(gh, 5, "c" * 40, [check("lint", "FAILURE")], linear_issue="ENG-101")

        for n in range(3):
            run_watcher(gh, ws, [REPO], linear=linear, now=T0 + n * RUN_INTERVAL)

        assert [c["issue_id"] for c in linear.comments] == ["ENG-101"]
        assert f"{REPO}" in linear.comments[0]["body"]
        assert "#5" in linear.comments[0]["body"]
        assert "CODE_FAILURE" in linear.comments[0]["body"]

    def test_unlinked_pr_gets_a_digest_event_and_no_linear_comment(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches a Linear comment being sent when no story is linked."""
        open_pr(gh, 6, "d" * 40, [check("lint", "FAILURE")])

        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert kinds(result) == ["new_failure"]
        assert linear.comments == []

    def test_linear_comment_carries_state_written_before_it_is_sent(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches a comment sent before the digest and Linear events are durable."""
        open_pr(gh, 8, "8" * 40, [check("lint", "FAILURE")], linear_issue="ENG-102")
        seen: list[dict[str, Any]] = []
        linear._before_comment = lambda: seen.append(disk(ws))

        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        key = result["notifications"][0]["idempotency_key"]
        assert len(seen) == 1
        assert list(seen[0]["digest_events"]) == [key]
        assert [e["linear_issue_id"] for e in seen[0]["pending_events"]] == ["ENG-102"]
        assert disk(ws)["pending_events"] == []

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("RETRY", "new_failure"),
            ("REPAIR", "new_failure"),
            ("BLOCKED", "blocked"),
            ("READY", None),
            ("NO_ACTION", None),
        ],
    )
    def test_only_failure_actions_map_to_a_digest_event_type(
        self, action: str, expected: str | None
    ) -> None:
        """Catches a healthy or pending action being given an event type."""
        assert transition_event_type(action) == expected


# ============================================================================
# Requirement 9: Malformed or unreadable durable state fails closed
# ============================================================================


def valid_state(**overrides: Any) -> str:
    state: dict[str, Any] = {
        "schema_version": 1,
        "merged_cursors": {},
        "last_run_at": None,
        "pr_states": {},
        "rerun_attempts": {},
        "records": {"repair": [], "block": [], "regression": []},
        "pending_events": [],
        "digest_events": {},
        "digest_unwritten": [],
    }
    state.update(overrides)
    return json.dumps(state)


MALFORMED_STATES = {
    "not json": "{invalid json!!!",
    "empty file": "",
    "not an object": "[]",
    "required fields missing": json.dumps({"schema_version": 1}),
    "unsupported schema version": valid_state(schema_version=99),
    "attempts not a mapping": valid_state(rerun_attempts=[]),
    "negative attempt count": valid_state(rerun_attempts={"org/repo:pr1:a:lint:job-1": -1}),
    "record kind missing": valid_state(records={"repair": [], "block": []}),
    "cursor not a timestamp": valid_state(merged_cursors={"org/repo": "yesterday"}),
    "cursor without a timezone": valid_state(merged_cursors={"org/repo": "2026-03-02T12:00:00"}),
    "cursors not a mapping": valid_state(merged_cursors="2026-03-02T12:00:00Z"),
    "last run not a timestamp": valid_state(last_run_at="yesterday"),
    "unwritten digest key unknown": valid_state(digest_unwritten=["digest:gone"]),
}


class TestMalformedState:
    """Requirement: Malformed or unreadable durable state fails closed."""

    def test_valid_state_shape_loads(self, ws: WatcherState) -> None:
        """Catches the fixture builder itself being malformed, which would void the cases below."""
        ws.state_file.write_text(valid_state())
        assert ws.load()["schema_version"] == 1

    @pytest.mark.parametrize("payload", MALFORMED_STATES.values(), ids=MALFORMED_STATES.keys())
    def test_malformed_state_ends_the_run_with_no_write_rerun_or_notification(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient, payload: str
    ) -> None:
        """Catches a damaged state file being read as empty, which would reset every budget."""
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")], linear_issue="ENG-1")
        ws.state_file.write_text(payload)

        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert set(result) == {"ok", "error", "type"}
        assert result["ok"] is False
        assert result["type"] == "malformed_state"
        assert "pr_watcher_state.json" in result["error"]
        assert ws.state_file.read_text() == payload
        assert gh.rerun_calls == []
        assert linear.attempts == []

    def test_unreadable_state_fails_closed(self, gh: FakeGitHubClient, ws: WatcherState) -> None:
        """Catches an unreadable state path being treated as a missing one."""
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")])
        ws.state_file.mkdir()

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert result["type"] == "malformed_state"
        assert gh.rerun_calls == []
        assert ws.state_file.is_dir()

    def test_load_raises_for_a_corrupt_file(self, ws: WatcherState) -> None:
        """Catches load() swallowing a parse error and returning a fresh state."""
        ws.state_file.write_text("{invalid json!!!")
        with pytest.raises(MalformedStateError, match="malformed state"):
            ws.load()


# ============================================================================
# Requirement 10: Failed Linear notification is durable and retried
# ============================================================================


class TestFailedLinearRetry:
    """Requirement: Failed Linear notification is durable and retried."""

    def test_failed_comment_stays_durable_and_is_retried_until_it_lands(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches a failed comment being dropped, or re-sent after it finally lands."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")], linear_issue="ENG-7")
        linear._set_fail("ENG-7")

        run_watcher(gh, ws, [REPO], linear=linear, now=T0)
        run_watcher(gh, ws, [REPO], linear=linear, now=T0 + RUN_INTERVAL)
        pending = disk(ws)["pending_events"]
        assert [(e["linear_issue_id"], e["retry_count"]) for e in pending] == [("ENG-7", 2)]
        assert linear.comments == []

        linear._clear_fail()
        for n in (2, 3, 4):
            run_watcher(gh, ws, [REPO], linear=linear, now=T0 + n * RUN_INTERVAL)

        assert [c["issue_id"] for c in linear.comments] == ["ENG-7"]
        assert linear.attempts == ["ENG-7"] * 3
        assert disk(ws)["pending_events"] == []

    def test_retries_stop_after_five_runs_and_the_event_is_kept(
        self, gh: FakeGitHubClient, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches unbounded retries, and an exhausted event vanishing from state."""
        open_pr(gh, 1, "a" * 40, [check("lint", "FAILURE")], linear_issue="ENG-7")
        linear._set_fail("ENG-7")

        results = [
            run_watcher(gh, ws, [REPO], linear=linear, now=T0 + n * RUN_INTERVAL) for n in range(8)
        ]

        assert linear.attempts == ["ENG-7"] * 5
        pending = disk(ws)["pending_events"]
        assert [(e["linear_issue_id"], e["retry_count"]) for e in pending] == [("ENG-7", 5)]
        assert [len(r["pending_events"]) for r in results] == [1] * 8

    def test_deliver_reports_sent_and_remaining_separately(self) -> None:
        """Catches a delivered event being left queued, or a failed one being lost."""
        linear = FakeLinearClient()
        linear._set_fail("ENG-2")
        events = [
            build_linear_event("ENG-1", digest_event("new_failure", 1)),
            build_linear_event("ENG-2", digest_event("new_failure", 2)),
        ]

        remaining, sent = deliver_linear_events(events, linear)

        assert [e["linear_issue_id"] for e in sent] == ["ENG-1"]
        assert [(e["linear_issue_id"], e["retry_count"]) for e in remaining] == [("ENG-2", 1)]
        assert events[1]["retry_count"] == 0


# ============================================================================
# Requirement 11: State keys are path-safe and reject traversal
# ============================================================================


UNSAFE_KEYS = {
    "leading traversal": "../etc/passwd",
    "embedded traversal": "org/../../etc",
    "traversal inside a segment": "org/repo:pr1:..",
    "absolute path": "/etc/passwd",
    "backslash": "org\\repo",
    "empty segment": "org//repo",
    "empty": "",
    "null byte": "repo\x00name",
    "newline": "repo\nname",
}


class TestPathSafeKeys:
    """Requirement: State keys are path-safe and reject traversal."""

    @pytest.mark.parametrize("key", UNSAFE_KEYS.values(), ids=UNSAFE_KEYS.keys())
    def test_unsafe_key_is_rejected(self, key: str) -> None:
        """Catches a traversal, absolute or control-character key being accepted."""
        with pytest.raises(UnsafeKeyError):
            _safe_key(key)

    @pytest.mark.parametrize("name", REAL_CHECK_NAMES)
    def test_real_check_names_are_valid_key_material(self, name: str) -> None:
        """Catches key validation treating a space, parenthesis or slash as unsafe."""
        key = f"{REPO}:pr1:{'a' * 40}:{name}:job-1"
        assert _safe_key(key) == key

    @pytest.mark.parametrize("repo", ["../evil", "/abs/repo"])
    def test_unsafe_repository_fails_the_run_closed_with_nothing_persisted(
        self, gh: FakeGitHubClient, ws: WatcherState, repo: str
    ) -> None:
        """Catches an unsafe key reaching the state file, or the run acting anyway."""
        open_pr(gh, 1, "a" * 40, [check("lint", "CANCELLED", job_id="job-1")], repo=repo)

        result = run_watcher(gh, ws, [repo], now=T0)

        assert result["type"] == "unsafe_state_key"
        assert repo in result["error"]
        assert gh.rerun_calls == []
        # spec.md "State keys are path-safe": no state is persisted.
        assert not ws.state_file.exists()

    def test_unsafe_check_name_from_github_fails_the_run_closed(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches untrusted check text being used as key material without validation."""
        open_pr(gh, 1, "a" * 40, [check("lint/../../x", "CANCELLED", job_id="job-1")])

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert result["type"] == "unsafe_state_key"
        assert gh.rerun_calls == []
        # spec.md "State keys are path-safe": no state is persisted.
        assert not ws.state_file.exists()

    @pytest.mark.parametrize("unsafe", ["repository name", "check name"])
    def test_unsafe_key_beside_a_healthy_repo_fails_the_whole_run_closed(
        self, gh: FakeGitHubClient, ws: WatcherState, unsafe: str
    ) -> None:
        """Catches an unsafe key being skipped while the run still saves state and acts.

        spec.md "State keys are path-safe and reject traversal": the operation
        fails closed and no state is persisted, so the healthy repository's
        rerun, records and notifications must not happen either.
        """
        if unsafe == "repository name":
            bad_repo = "../evil"
        else:
            bad_repo = "org/unsafe-check"
            open_pr(
                gh, 1, "a" * 40, [check("lint/../../x", "CANCELLED", job_id="job-1")], repo=bad_repo
            )
        open_pr(gh, 2, "b" * 40, [check("lint", "CANCELLED", job_id="job-2")], repo=REPO)
        ws.save(ws.set_last_run(ws.load(), T0 - RUN_INTERVAL))
        before = ws.state_file.read_bytes()

        result = run_watcher(gh, ws, [bad_repo, REPO], now=T0)

        assert result["ok"] is False
        assert result["type"] == "unsafe_state_key"
        assert bad_repo in result["error"]
        assert [e["kind"] for e in result["repo_errors"]] == ["unsafe_state_key"]
        assert result["reruns"] == []
        assert result["records"] == []
        assert result["notifications"] == []
        assert gh.rerun_calls == []
        assert ws.state_file.read_bytes() == before
        assert not ws.digest_file.exists()

    def test_state_writers_reject_an_unsafe_key(self, ws: WatcherState) -> None:
        """Catches a writer that stores a key without validating it."""
        data = ws.load()
        with pytest.raises(UnsafeKeyError):
            ws.set_rerun_attempt(data, "org/repo:pr1:../x", 1)
        with pytest.raises(UnsafeKeyError):
            ws.add_record(data, {"type": "repair", "idempotency_key": "repair:../x"})
        with pytest.raises(UnsafeKeyError):
            RepairRecord(repo="../x", pr_number=1, head="a" * 40, failing_checks=[])


# ============================================================================
# Requirement 13: Durable state is written before side effects
# ============================================================================


class Killed(BaseException):
    """Stands in for the process dying: nothing in the watcher may catch it."""


class TestStateBeforeSideEffects:
    """Requirement: Durable state is written before side effects."""

    def test_rerun_is_requested_only_after_the_whole_run_is_durable(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a rerun sent before the counter, classification, transition or cursor is saved."""
        head = "a" * 40
        open_pr(gh, 42, head, [check("proof-full", "CANCELLED", job_id="job-1")])
        seen: list[dict[str, Any]] = []
        gh._before_rerun = lambda: seen.append(disk(ws))

        run_watcher(gh, ws, [REPO], now=T0)

        assert len(seen) == 1
        assert seen[0]["rerun_attempts"] == {f"{REPO}:pr42:{head}:proof-full:job-1": 1}
        assert seen[0]["pr_states"][f"state:{REPO}:pr42:{head}"]["classification"] == "CANCELLED"
        assert [e["type"] for e in seen[0]["digest_events"].values()] == ["new_failure"]
        assert seen[0]["merged_cursors"] == {REPO: iso(T0)}

    def test_death_between_state_write_and_rerun_never_exceeds_the_bound(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a restarted watcher re-taking an attempt it had already written down."""
        head = "a" * 40
        open_pr(gh, 42, head, [check("proof-full", "CANCELLED", job_id="job-1")])

        def die() -> None:
            raise Killed

        gh._before_rerun = die
        with pytest.raises(Killed):
            run_watcher(gh, ws, [REPO], now=T0)
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr42:{head}:proof-full:job-1": 1}
        assert gh.rerun_calls == []

        gh._before_rerun = None
        later = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in (1, 2, 3)]

        assert gh.rerun_calls == []
        assert [r["observations"][0]["action"] for r in later] == ["BLOCKED"] * 3

    def test_failed_state_write_reports_the_error_and_causes_no_side_effect(
        self,
        gh: FakeGitHubClient,
        ws: WatcherState,
        linear: FakeLinearClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Catches a rerun or comment going out when the state could not be written."""
        open_pr(
            gh,
            1,
            "a" * 40,
            [check("proof-full", "CANCELLED", job_id="job-1")],
            linear_issue="ENG-1",
        )

        def disk_full(src: str, dst: str) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("verdict.pr_watcher_state.os.replace", disk_full)
        result = run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert result["type"] == "state_write_failed"
        assert "No space left on device" in result["error"]
        assert gh.rerun_calls == []
        assert linear.attempts == []
        assert not ws.state_file.exists()


# ============================================================================
# Requirement 14: Disagreeing review evidence
# ============================================================================


class TestDisagreeingEvidence:
    """Requirement: Disagreeing review evidence is recorded and treated as fail."""

    def test_approved_review_with_keyed_fail_receipt_is_a_regression_naming_both(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches an APPROVED review overriding a recorded Verdict/ocr fail."""
        head = "d" * 40
        gh._set_merged_prs(REPO, [{"number": 99, "head_sha": head, "merged_at": MERGED_AT}])
        gh._set_reviews(REPO, 99, [{"state": "APPROVED", "id": 1}])
        receipts = {f"{REPO}:99:{head}": {"status": "FAIL"}}

        result = run_watcher(gh, ws, [REPO], verdict_fail_receipts=receipts, now=T0)

        assert disk(ws)["records"]["regression"] == [
            {
                "type": "regression",
                "repo": REPO,
                "pr_number": 99,
                "head": head,
                "review_sources": ["verdict_ocr_fail", "disagreeing_evidence"],
                "reason": "merged PR had fail review evidence",
                "idempotency_key": f"regression:{REPO}:pr99:{head}",
                "created_at": iso(T0),
            }
        ]
        assert kinds(result) == ["regression"]
        assert gh.rerun_calls == []


# ============================================================================
# Requirement 15: Idle runs make no model call
# ============================================================================


WATCHER_MODULES = (
    "verdict.ci_policy",
    "verdict.pr_watcher",
    "verdict.pr_watcher_state",
    "verdict.pr_watcher_github",
    "verdict.pr_watcher_notifications",
)
MODEL_PATH_MODULES = (
    "verdict.router",
    "verdict.intelligence",
    "verdict.relay",
    "verdict.proxy",
    "verdict.api",
    "httpx",
    "requests",
    "openai",
    "anthropic",
)


def refuse(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the watcher opened a network or process channel")


class TestNoModelCall:
    """Requirement: Idle runs make no model call."""

    def test_watcher_import_closure_has_no_model_or_gateway_module(self) -> None:
        """Catches a routing, relay, gateway or provider-SDK import reaching the watcher."""
        script = (
            "import json, sys\n"
            + "".join(f"import {name}\n" for name in WATCHER_MODULES)
            + "print(json.dumps(sorted(sys.modules)))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        loaded = set(json.loads(done.stdout))

        assert set(WATCHER_MODULES) <= loaded
        reached = {
            name
            for name in loaded
            if name in MODEL_PATH_MODULES
            or name.startswith("verdict.gateway")
            or any(name.startswith(f"{banned}.") for banned in MODEL_PATH_MODULES)
        }
        assert reached == set()

    def test_idle_run_opens_no_socket_or_process_and_emits_nothing(
        self, gh: FakeGitHubClient, ws: WatcherState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Catches an idle run that reaches a gateway, a provider, or the gh CLI."""
        open_pr(gh, 1, "9" * 40, [check("proof-full"), check("lint")])
        run_watcher(gh, ws, [REPO], now=T0)

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(subprocess, "Popen", refuse)
        result = run_watcher(gh, ws, [REPO], now=T0 + RUN_INTERVAL)

        assert [o["action"] for o in result["observations"]] == ["READY"]
        assert result["notifications"] == []
        assert result["reruns"] == []
        assert result["records"] == []


# ============================================================================
# Regression tests for watcher core fixes
# ============================================================================


class TestInfraNamedFailure:
    """A failure the classifier calls infrastructure is rerun once, then blocked."""

    def test_infra_failure_is_rerun_once_and_blocked_once_across_five_runs(
        self, gh: FakeGitHubClient, ws: WatcherState
    ) -> None:
        """Catches a rerun on every poll, and a block record on every poll after it."""
        head = "a" * 40
        open_pr(gh, 1, head, [check(INFRA_NAMED_CHECK, "FAILURE", job_id="job-1")])

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(5)]

        assert [r["observations"][0]["classification"] for r in runs] == ["INFRA_FAILURE"] * 5
        assert gh.rerun_calls == [{"job_id": "job-1", "repo": REPO}]
        state = disk(ws)
        assert state["rerun_attempts"] == {f"{REPO}:pr1:{head}:{INFRA_NAMED_CHECK}:job-1": 1}
        assert [b["idempotency_key"] for b in state["records"]["block"]] == [
            f"block:{REPO}:pr1:{head}"
        ]


# ============================================================================
# Additional: Record type tests
# ============================================================================


class TestRecordTypes:
    """Each record carries its identity and a key that is stable per head."""

    def test_block_record(self) -> None:
        """Catches a block record losing its reason or its per-head identity."""
        record = BlockRecord(REPO, 1, "a" * 40, "attempts exhausted for CANCELLED")
        assert record.to_dict(iso(T0)) == {
            "type": "block",
            "repo": REPO,
            "pr_number": 1,
            "head": "a" * 40,
            "reason": "attempts exhausted for CANCELLED",
            "idempotency_key": f"block:{REPO}:pr1:{'a' * 40}",
            "created_at": iso(T0),
        }

    def test_record_keys_differ_by_kind_pr_and_head(self) -> None:
        """Catches two different records colliding on one idempotency key."""
        keys = {
            BlockRecord(REPO, 1, "a" * 40, "r").idempotency_key,
            BlockRecord(REPO, 2, "a" * 40, "r").idempotency_key,
            BlockRecord(REPO, 1, "b" * 40, "r").idempotency_key,
            RepairRecord(REPO, 1, "a" * 40, []).idempotency_key,
            RegressionRecord(REPO, 1, "a" * 40, [], "r").idempotency_key,
        }
        assert len(keys) == 5


# ============================================================================
# Additional: Notification tests
# ============================================================================


def digest_event(event_type: str, pr_number: int) -> dict[str, Any]:
    return build_digest_event(
        event_type, REPO, pr_number, "a" * 40, "CODE_FAILURE", "lint failed", iso(T0)
    )


class TestNotificationBuild:
    """Verify digest and Linear event construction."""

    def test_digest_key_separates_type_pr_head_and_classification(self) -> None:
        """Catches two distinct transitions collapsing onto one idempotency key."""
        base = build_digest_event("new_failure", REPO, 42, "a" * 40, "CODE_FAILURE", None, iso(T0))
        variants = [
            build_digest_event("blocked", REPO, 42, "a" * 40, "CODE_FAILURE", None, iso(T0)),
            build_digest_event("new_failure", REPO, 43, "a" * 40, "CODE_FAILURE", None, iso(T0)),
            build_digest_event("new_failure", REPO, 42, "b" * 40, "CODE_FAILURE", None, iso(T0)),
            build_digest_event("new_failure", REPO, 42, "a" * 40, "CONFLICT", None, iso(T0)),
        ]
        keys = {base["idempotency_key"], *(v["idempotency_key"] for v in variants)}
        assert len(keys) == 5
        assert base["idempotency_key"] == f"digest:new_failure:{REPO}:pr42:{'a' * 40}:CODE_FAILURE"
        assert base["timestamp"] == iso(T0)

    def test_linear_event_names_the_linked_issue_and_mirrors_the_digest(self) -> None:
        """Catches the Linear event losing its issue, or its link to the digest event."""
        digest = digest_event("regression", 42)
        event = build_linear_event("ENG-123", digest)

        assert event["linear_issue_id"] == "ENG-123"
        assert event["idempotency_key"] == "linear:" + digest["idempotency_key"][len("digest:") :]
        assert event["retry_count"] == 0
        assert (event["repo"], event["pr_number"], event["head"]) == (REPO, 42, "a" * 40)


# ============================================================================
# The concrete gh client, against synthetic GraphQL payload fixtures
# ============================================================================

FIXTURES = Path(__file__).parent / "fixtures" / "pr_watcher"
HEAD_5, HEAD_6, HEAD_7, HEAD_8, HEAD_10 = "c3" * 20, "d4" * 20, "a1" * 20, "b2" * 20, "f6" * 20
CURSOR_OPEN = "Y3Vyc29yOnYyOpHOAAAAAQ=="
CURSOR_CONTEXTS = "Y3Vyc29yOnYyOpHOAAAAAg=="
CURSOR_MERGED = "Y3Vyc29yOjE="
QUERY_KINDS = {OPEN_PULLS_QUERY: "open", PULL_QUERY: "pull", MERGED_PULLS_QUERY: "merged"}
Route = tuple[str, str | None, str | None]

TWO_PAGE_ROUTES: dict[Route, Any] = {
    ("open", None, None): "open_pulls_page1.json",
    ("open", None, CURSOR_OPEN): "open_pulls_page2.json",
    ("pull", "7", None): "pull_7_contexts_page1.json",
    ("pull", "7", CURSOR_CONTEXTS): "pull_7_contexts_page2.json",
    ("pull", "8", None): "pull_8.json",
    ("merged", None, None): "merged_pulls_page1.json",
    ("merged", None, CURSOR_MERGED): "merged_pulls_page2.json",
}
CANCELLED_ROUTES: dict[Route, Any] = {
    ("open", None, None): "open_pulls_single.json",
    ("pull", "10", None): "pull_10_cancelled.json",
    ("merged", None, None): "merged_pulls_empty.json",
}


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return loaded


JOB_RERUN_PATH = re.compile(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/jobs/[0-9]+/rerun")


def assert_read_or_job_rerun(argv: list[str]) -> None:
    """Fail unless a ``gh api`` argv is a GraphQL read or the one Actions job rerun.

    Every call the concrete client makes in this file passes through here.
    """
    if argv[0] == "graphql":
        flags, items = argv[1::2], argv[2::2]
        assert len(flags) == len(items) and set(flags) <= {"-f", "-F"}, argv
        assert all("=" in item for item in items), argv
        fields = dict(item.split("=", 1) for item in items)
        assert fields["query"].startswith("query("), argv
        assert "mutation" not in fields["query"], argv
        return
    assert argv[:2] == ["--method", "POST"] and len(argv) == 3, argv
    assert JOB_RERUN_PATH.fullmatch(argv[2]), argv


class FixtureGh(_GhApiClient):
    """The real client; only the process call is replaced, by payload fixtures."""

    def __init__(self, routes: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.routes = routes
        self.calls: list[list[str]] = []

    def _call(self, *args: str) -> str:
        assert_read_or_job_rerun(list(args))
        self.calls.append(list(args))
        if args[0] != "graphql":
            return ""
        fields = dict(a.split("=", 1) for a in args[1:] if a not in ("-f", "-F"))
        kind = QUERY_KINDS[fields["query"]]
        payload = self.routes[(kind, fields.get("number"), fields.get("after"))]
        if isinstance(payload, dict):
            return json.dumps(payload)
        # A string is a fixture file name, or a raw response body to return as-is.
        return (FIXTURES / payload).read_text() if payload.endswith(".json") else str(payload)


def graphql_argv(query: str, *fields: str) -> list[str]:
    """The argv after ``gh api`` for one read: -f for strings, -F for the integers."""
    argv = ["graphql", "-f", f"query={query}"]
    for item in fields:
        name = item.split("=", 1)[0]
        argv += ["-F" if name in ("first", "number") else "-f", item]
    return argv


def actions_check(
    name: str, conclusion: str | None, job: int, run: int, required: bool
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "required": required,
        "job_id": job,
        "workflow_run_id": run,
        "details_url": f"https://github.com/org/repo/actions/runs/{run}/job/{job}",
        "started_at": "2026-03-02T11:40:00Z",
    }


class TestGhClientReads:
    """The client turns GitHub's GraphQL payloads into the dicts the core consumes."""

    def test_open_pull_requests_are_read_across_both_pages(self) -> None:
        """Catches the second page of open PRs being dropped, or a wrong gh invocation."""
        gh = FixtureGh(TWO_PAGE_ROUTES)

        assert gh.list_open_prs(REPO) == [
            {
                "number": 7,
                "head_sha": HEAD_7,
                "mergeable": "MERGEABLE",
                "merge_state_status": "BLOCKED",
            },
            {
                "number": 8,
                "head_sha": HEAD_8,
                "mergeable": "MERGEABLE",
                "merge_state_status": "CLEAN",
            },
        ]
        first = f"first={OPEN_PULLS_PAGE_SIZE}"
        assert gh.calls == [
            graphql_argv(OPEN_PULLS_QUERY, "owner=org", "name=repo", first),
            graphql_argv(OPEN_PULLS_QUERY, "owner=org", "name=repo", first, f"after={CURSOR_OPEN}"),
        ]

    def test_pull_request_checks_are_read_across_both_context_pages(self) -> None:
        """Catches checks past the first page being lost, or GitHub's fields being misread."""
        gh = FixtureGh(TWO_PAGE_ROUTES)

        assert gh.get_pr(REPO, 7) == {
            "number": 7,
            "head_sha": HEAD_7,
            "commit_oid": HEAD_7,
            "mergeable": "MERGEABLE",
            "merge_state_status": "BLOCKED",
            "linear_issue": "BOD-195",
            "checks": [
                actions_check("lint", "FAILURE", 9001, 501, True),
                actions_check("test (3.11)", "SUCCESS", 9002, 501, True),
                {
                    "name": "Analyze (python)",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "required": False,
                    "job_id": None,
                    "workflow_run_id": None,
                    "details_url": "https://github.com/org/repo/actions/runs/0/job/9003",
                    "started_at": "2026-03-02T11:40:00Z",
                },
                {
                    "name": "license/cla",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                    "required": False,
                    "job_id": None,
                    "workflow_run_id": None,
                    "details_url": None,
                    "started_at": None,
                },
            ],
        }
        first = f"first={CONTEXTS_PAGE_SIZE}"
        base = ("owner=org", "name=repo", "number=7", first)
        assert gh.calls == [
            graphql_argv(PULL_QUERY, *base),
            graphql_argv(PULL_QUERY, *base, f"after={CURSOR_CONTEXTS}"),
        ]

    def test_pull_request_with_no_check_rollup_has_no_checks(self) -> None:
        """Catches a PR with no checks at all crashing the client instead of reading as empty."""
        gh = FixtureGh({("pull", "9", None): "pull_9_no_checks.json"})

        pr = gh.get_pr(REPO, 9)

        assert (pr["checks"], pr["mergeable"], pr["merge_state_status"]) == (
            [],
            "UNKNOWN",
            "UNKNOWN",
        )
        assert (pr["head_sha"], pr["commit_oid"], pr["linear_issue"]) == (HEAD_8, HEAD_8, None)

    @pytest.mark.parametrize("moved", ["headRefOid", "commit"])
    def test_head_that_moves_between_context_pages_yields_no_checks_and_no_commit(
        self, moved: str
    ) -> None:
        """Catches checks of two different commits being merged into one list."""
        second = fixture("pull_7_contexts_page2.json")
        node = second["data"]["repository"]["pullRequest"]
        if moved == "headRefOid":
            node["headRefOid"] = "99" * 20
        node["commits"]["nodes"][0]["commit"]["oid"] = "99" * 20
        gh = FixtureGh({**TWO_PAGE_ROUTES, ("pull", "7", CURSOR_CONTEXTS): second})

        pr = gh.get_pr(REPO, 7)

        assert (pr["commit_oid"], pr["checks"]) == ("", [])
        assert pr["head_sha"] == ("99" * 20 if moved == "headRefOid" else HEAD_7)

    def test_commit_that_differs_from_the_head_is_reported_as_such(self) -> None:
        """Catches the checks' commit being assumed to be the head instead of being read."""
        first = fixture("pull_8.json")
        first["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["oid"] = (
            "77" * 20
        )
        gh = FixtureGh({("pull", "8", None): first})

        pr = gh.get_pr(REPO, 8)

        assert (pr["head_sha"], pr["commit_oid"]) == (HEAD_8, "77" * 20)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("SUCCESS", ("COMPLETED", "SUCCESS")),
            ("FAILURE", ("COMPLETED", "FAILURE")),
            ("ERROR", ("COMPLETED", "FAILURE")),
            ("PENDING", ("IN_PROGRESS", None)),
            ("EXPECTED", ("IN_PROGRESS", None)),
            ("SOMETHING_NEW", ("IN_PROGRESS", None)),
        ],
    )
    def test_commit_status_state_maps_to_a_check_status_and_conclusion(
        self, state: str, expected: tuple[str, str | None]
    ) -> None:
        """Catches a commit status that failed, or is unknown, reading as passed."""
        page = fixture("pull_7_contexts_page2.json")
        contexts = page["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
        contexts["statusCheckRollup"]["contexts"]["nodes"][1]["state"] = state
        gh = FixtureGh({("pull", "7", None): page})

        status = gh.get_pr(REPO, 7)["checks"][1]

        assert (status["name"], status["status"], status["conclusion"]) == (
            "license/cla",
            *expected,
        )

    def test_merged_pull_requests_are_searched_across_both_pages_with_their_reviews(self) -> None:
        """Catches merged PRs past the first page being missed, or a review losing its commit."""
        gh = FixtureGh(TWO_PAGE_ROUTES)
        since = "2026-03-01T12:00:00Z"

        assert gh.list_merged_prs(REPO, since) == [
            {
                "number": 5,
                "head_sha": HEAD_5,
                "merged_at": "2026-03-02T11:58:00Z",
                "linear_issue": "BOD-286",
                "reviews": [
                    {
                        "state": "COMMENTED",
                        "submitted_at": "2026-03-02T10:00:00Z",
                        "commit_oid": "e5" * 20,
                    },
                    {
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-03-02T10:30:00Z",
                        "commit_oid": HEAD_5,
                    },
                ],
            },
            {
                "number": 6,
                "head_sha": HEAD_6,
                "merged_at": "2026-03-02T11:59:00Z",
                "linear_issue": "SHA-256",
                "reviews": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-03-02T09:00:00Z",
                        "commit_oid": None,
                    },
                    {
                        "state": "APPROVED",
                        "submitted_at": "2026-03-02T11:00:00Z",
                        "commit_oid": HEAD_6,
                    },
                ],
            },
        ]
        q = f"q=repo:org/repo is:pr is:merged merged:>={since}"
        first = f"first={MERGED_PULLS_PAGE_SIZE}"
        assert gh.calls == [
            graphql_argv(MERGED_PULLS_QUERY, q, first),
            graphql_argv(MERGED_PULLS_QUERY, q, first, f"after={CURSOR_MERGED}"),
        ]

    def test_search_node_that_is_not_a_pull_request_is_skipped(self) -> None:
        """Catches an empty node, which GitHub returns for a non-PR hit, crashing the listing."""
        page = fixture("merged_pulls_page2.json")
        page["data"]["search"]["nodes"].insert(0, {})
        gh = FixtureGh({("merged", None, None): page})

        assert [p["number"] for p in gh.list_merged_prs(REPO, "2026-03-01T12:00:00Z")] == [6]


class TestGhClientLimitsAndErrors:
    """The client is bounded, and anything it cannot trust is a GitHubClientError."""

    @pytest.mark.parametrize("call", ["open", "pull", "merged"])
    def test_listing_longer_than_the_page_cap_is_refused_not_truncated(self, call: str) -> None:
        """Catches an endless or oversized listing looping for ever, or being silently cut."""
        endless = {
            "open": fixture("open_pulls_page1.json"),
            "pull": fixture("pull_7_contexts_page1.json"),
            "merged": fixture("merged_pulls_page1.json"),
        }[call]
        gh = FixtureGh(collections.defaultdict(lambda: endless))

        with pytest.raises(GitHubClientError, match=f"more than {MAX_PAGES} pages"):
            if call == "open":
                gh.list_open_prs(REPO)
            elif call == "pull":
                gh.get_pr(REPO, 7)
            else:
                gh.list_merged_prs(REPO, "2026-03-01T12:00:00Z")

        assert (MAX_PAGES, len(gh.calls)) == (20, 20)

    @pytest.mark.parametrize(
        "payload",
        [
            "<html>502 Bad Gateway</html>",
            json.dumps([]),
            json.dumps({"errors": [{"message": "API rate limit exceeded"}], "data": None}),
            json.dumps({"data": {"repository": None}}),
            json.dumps({"data": {"repository": {"pullRequests": {"nodes": [{"number": 1}]}}}}),
        ],
        ids=["not json", "not an object", "graphql errors", "no repository", "missing fields"],
    )
    def test_untrustworthy_response_is_a_client_error(self, payload: str) -> None:
        """Catches a broken response being read as 'no open pull requests'."""
        gh = FixtureGh(collections.defaultdict(lambda: payload))

        with pytest.raises(GitHubClientError):
            gh.list_open_prs(REPO)

    def test_graphql_error_message_reaches_the_caller(self) -> None:
        """Catches the reason for a failed read being lost."""
        payload = json.dumps({"errors": [{"message": "API rate limit exceeded"}], "data": None})
        gh = FixtureGh(collections.defaultdict(lambda: payload))

        with pytest.raises(GitHubClientError, match="API rate limit exceeded"):
            gh.get_pr(REPO, 7)

    @pytest.mark.parametrize(
        "repo", ["org", "org/repo/extra", "org/..", "../repo", "org/re po", "org/repo is:open", ""]
    )
    def test_malformed_repository_is_refused_before_any_call(self, repo: str) -> None:
        """Catches a repository string reaching a search query or a REST path unchecked."""
        gh = FixtureGh(TWO_PAGE_ROUTES)

        for call in (
            lambda: gh.list_open_prs(repo),
            lambda: gh.get_pr(repo, 7),
            lambda: gh.list_merged_prs(repo, "2026-03-01T12:00:00Z"),
            lambda: gh.rerun_check(9001, repo),
        ):
            with pytest.raises(GitHubClientError, match="repository"):
                call()

        assert gh.calls == []

    @pytest.mark.parametrize(
        "since", ["", "yesterday", "2026-03-01", "2026-03-01T12:00:00Z is:open"]
    )
    def test_malformed_since_is_refused_before_any_call(self, since: str) -> None:
        """Catches a bad lower bound turning the merged search into an unbounded one."""
        gh = FixtureGh(TWO_PAGE_ROUTES)

        with pytest.raises(GitHubClientError, match="since"):
            gh.list_merged_prs(REPO, since)

        assert gh.calls == []


class TestGhClientProcess:
    """How the client runs gh: argv, timeout and failures."""

    def test_gh_is_run_with_a_timeout_and_without_a_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Catches a call that can hang the watcher, or one built as a shell string."""
        seen: list[tuple[list[str], dict[str, Any]]] = []

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert_read_or_job_rerun(argv[2:])
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        monkeypatch.setattr("verdict.pr_watcher_github.subprocess.run", run)
        query = f"query={OPEN_PULLS_QUERY}"

        assert _GhApiClient()._call("graphql", "-f", query) == "{}"
        assert seen == [
            (
                ["gh", "api", "graphql", "-f", query],
                {"capture_output": True, "text": True, "check": True, "timeout": 30},
            )
        ]
        assert CALL_TIMEOUT_SECONDS == 30

    @pytest.mark.parametrize(
        ("failure", "message"),
        [
            (
                subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found (HTTP 404)\nusage"),
                "gh: Not Found \\(HTTP 404\\)$",
            ),
            (subprocess.CalledProcessError(4, ["gh"], stderr=""), "exit 4"),
            (subprocess.TimeoutExpired(["gh"], 30), "timed out after 30s"),
            (FileNotFoundError(2, "No such file or directory", "gh"), "could not be run"),
        ],
        ids=["http error", "silent failure", "timeout", "gh missing"],
    )
    def test_failed_gh_call_is_a_client_error(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str
    ) -> None:
        """Catches a gh failure escaping as an exception the watcher does not isolate."""

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert argv[:2] == ["gh", "api"]
            assert_read_or_job_rerun(argv[2:])
            raise failure

        monkeypatch.setattr("verdict.pr_watcher_github.subprocess.run", run)

        with pytest.raises(GitHubClientError, match=message):
            _GhApiClient().list_open_prs(REPO)


class TestLinearIssueLink:
    """A PR's Linear story is read from its branch name, else from its body."""

    @pytest.mark.parametrize(
        ("branch", "body", "expected"),
        [
            ("chore/bod-195-security-deps", "", "BOD-195"),
            ("bod-102-evidence", "", "BOD-102"),
            ("fix/BOD-286", "", "BOD-286"),
            ("chore/bod-195-security-deps", "Relates to ENG-9.", "BOD-195"),
            ("feat/pr-watcher", "Fixes ENG-9 and ENG-10.", "ENG-9"),
            ("feat/pr-watcher", "Adds the watcher core.", None),
            ("feat/pr-watcher", "", None),
            ("dependabot/pip/pytest-cov-gte-7.1.0", "", None),
            ("dependabot/pip/opentelemetry-exporter-otlp-1.45.2", "", None),
            ("checkpoint/337-eligibility-filtering-20260905", "", None),
            ("cursor/cloud-agent-1793584466375-l0k5u", "", None),
            ("bod275/boundary", "", None),
            ("feat/pr-watcher", "see eng-9 (lower case is not an identifier in prose)", None),
            ("feat/pr-watcher", "Tracks CVE-2026-1234.", None),
        ],
    )
    def test_issue_is_taken_from_the_branch_first_then_the_body(
        self, branch: str, body: str, expected: str | None
    ) -> None:
        """Catches a story being missed, or a version number in a branch being read as one."""
        assert linear_issue_for(branch, body) == expected

    @pytest.mark.parametrize("body", ["Digest is SHA-256.", "Encoded as UTF-8.", "See ADR-036."])
    def test_any_upper_case_identifier_in_the_body_is_taken_unless_team_keys_are_given(
        self, body: str
    ) -> None:
        """Catches the team-key restriction failing to stop a comment on a non-story.

        Without team keys the pattern cannot tell 'SHA-256' from a story, so it is
        taken; this pins that hazard as well as the restriction that removes it.
        """
        assert linear_issue_for("feat/pr-watcher", body) is not None
        assert linear_issue_for("feat/pr-watcher", body, ("BOD", "eng")) is None
        assert (
            linear_issue_for("feat/pr-watcher", f"{body} Fixes ENG-9.", ("BOD", "eng")) == "ENG-9"
        )

    def test_client_applies_its_team_keys_to_open_and_merged_pull_requests(self) -> None:
        """Catches the configured team keys being ignored by one of the two read paths."""
        gh = FixtureGh(TWO_PAGE_ROUTES, linear_team_keys=("ENG",))

        assert gh.get_pr(REPO, 7)["linear_issue"] == "ENG-9"
        assert [p["linear_issue"] for p in gh.list_merged_prs(REPO, "2026-03-01T12:00:00Z")] == [
            None,
            None,
        ]


class TestGhClientCapability:
    """The concrete client can read, and can rerun one Actions job. Nothing else."""

    def test_real_client_exposes_exactly_the_protocol_and_passes_the_capability_check(self) -> None:
        """Catches a merge, push or edit method being added to the client that ships."""
        client = create_github_client()

        assert type(client) is _GhApiClient
        assert {name for name in dir(client) if not name.startswith("_")} == {
            "list_open_prs",
            "get_pr",
            "list_merged_prs",
            "rerun_check",
        }
        assert_github_no_mutation(client)

    def test_every_read_is_a_graphql_query_and_the_only_write_is_the_job_rerun(self) -> None:
        """Catches any method issuing a mutation, a REST write, or a rerun by GET."""
        gh = FixtureGh(TWO_PAGE_ROUTES)

        gh.list_open_prs(REPO)
        gh.get_pr(REPO, 7)
        gh.list_merged_prs(REPO, "2026-03-01T12:00:00Z")
        assert gh.rerun_check(9001, REPO) == {"job_id": 9001, "requested": True}

        writes = [argv for argv in gh.calls if argv[0] != "graphql"]
        assert writes == [["--method", "POST", "repos/org/repo/actions/jobs/9001/rerun"]]
        assert len(gh.calls) == 7
        assert all("-s" not in argv and "-X" not in argv for argv in gh.calls)

    @pytest.mark.parametrize(
        "argv",
        [
            ["graphql", "-f", "query=mutation($id: ID!) { mergePullRequest(input: {}) { x } }"],
            ["graphql", "-f", "query=query($q: String!) { a } mutation { b }"],
            ["graphql", "--method", "DELETE", "-f", "query=query($q: String!) { a }"],
            ["repos/org/repo/pulls", "-s", "open"],
            ["repos/org/repo/actions/jobs/9001/rerun"],
            ["--method", "POST", "repos/org/repo/pulls/7/merge"],
            ["--method", "POST", "repos/org/repo/check-runs/9001/rerequest"],
            ["--method", "PUT", "repos/org/repo/actions/jobs/9001/rerun"],
            ["--method", "DELETE", "repos/org/repo/git/refs/heads/main"],
            ["--method", "POST", "repos/org/repo/actions/jobs/9001/rerun", "-f", "ref=main"],
            ["--method", "POST", "repos/org/repo/actions/jobs/9001/rerun/../../../merges"],
        ],
        ids=[
            "graphql mutation",
            "mutation after a query",
            "graphql with a method",
            "rest read with -s",
            "rerun by GET",
            "merge",
            "rerequest",
            "rerun by PUT",
            "delete a ref",
            "rerun with a body",
            "path escape",
        ],
    )
    def test_the_argv_scan_rejects_every_other_kind_of_call(self, argv: list[str]) -> None:
        """Catches the scan that guards every client call in this file being a no-op."""
        with pytest.raises(AssertionError):
            assert_read_or_job_rerun(argv)

    @pytest.mark.parametrize("job_id", [None, "", "9001/../../pulls/7/merge", "abc", True, -1, 1.5])
    def test_rerun_of_anything_but_a_numeric_job_id_makes_no_call(self, job_id: Any) -> None:
        """Catches a crafted job id turning the one permitted write into another endpoint."""
        gh = FixtureGh(TWO_PAGE_ROUTES)

        with pytest.raises(GitHubClientError, match="job id"):
            gh.rerun_check(job_id, REPO)

        assert gh.calls == []


class TestGhClientEndToEnd:
    """Payload fixtures through the real client and the whole watcher run."""

    def test_failed_required_lint_check_becomes_one_repair_record_and_one_digest_line(
        self, ws: WatcherState
    ) -> None:
        """Catches the client and the classifiers disagreeing about GitHub's field values.

        This is the path that silently did nothing with the earlier REST client.
        """
        gh = FixtureGh(TWO_PAGE_ROUTES)

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(2)]

        assert [r["ok"] for r in runs] == [True, True]
        assert outcomes(runs[0]) == [(7, "CODE_FAILURE", "REPAIR"), (8, "GREEN", "READY")]
        state = disk(ws)
        assert state["records"]["repair"] == [
            {
                "type": "repair",
                "repo": REPO,
                "pr_number": 7,
                "head": HEAD_7,
                "failing_checks": ["lint"],
                "failing_check_details": [
                    {
                        "name": "lint",
                        "conclusion": "FAILURE",
                        "job_id": 9001,
                        "details_url": "https://github.com/org/repo/actions/runs/501/job/9001",
                    }
                ],
                "classification": "CODE_FAILURE",
                "reason": "PR classified as CODE_FAILURE",
                "idempotency_key": f"repair:{REPO}:pr7:{HEAD_7}:CODE_FAILURE",
                "created_at": iso(T0),
            }
        ]
        assert [(r["pr_number"], r["review_sources"]) for r in state["records"]["regression"]] == [
            (5, ["github_changes_requested"])
        ]
        assert [(e["type"], e["pr_number"], e["classification"]) for e in digest_lines(ws)] == [
            ("new_failure", 7, "CODE_FAILURE"),
            ("regression", 5, "REGRESSION"),
        ]
        assert [kinds(r) for r in runs] == [["new_failure", "regression"], []]
        assert all(argv[0] == "graphql" for argv in gh.calls)
        merged_queries = [a for a in gh.calls if a[2] == f"query={MERGED_PULLS_QUERY}"]
        assert [a[4] for a in merged_queries] == [
            "q=repo:org/repo is:pr is:merged merged:>=2026-03-01T12:00:00Z",
            "q=repo:org/repo is:pr is:merged merged:>=2026-03-01T12:00:00Z",
            "q=repo:org/repo is:pr is:merged merged:>=2026-03-02T11:55:00Z",
            "q=repo:org/repo is:pr is:merged merged:>=2026-03-02T11:55:00Z",
        ]

    @pytest.mark.parametrize("moved", ["headRefOid", "commit"])
    def test_paginated_exact_head_observation(self, ws: WatcherState, moved: str) -> None:
        """Catches stale checks being acted on when a head moves after its page was listed.

        Spec scenario: two pages of open PRs; the PR on the second page gets a
        new head before its checks are consumed.  The stable PR is evaluated,
        the moved one is reported and left alone.
        """
        new_head = "99" * 20
        pull_8 = fixture("pull_8.json")
        node = pull_8["data"]["repository"]["pullRequest"]
        if moved == "headRefOid":
            node["headRefOid"] = new_head
        node["commits"]["nodes"][0]["commit"]["oid"] = new_head
        failing = node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
        failing["conclusion"] = "CANCELLED"
        gh = FixtureGh({**TWO_PAGE_ROUTES, ("pull", "8", None): pull_8})

        result = run_watcher(gh, ws, [REPO], now=T0)

        assert outcomes(result) == [(7, "CODE_FAILURE", "REPAIR"), (8, "HEAD_CHANGED", "NO_ACTION")]
        assert result["observations"][1]["head"] == HEAD_8
        assert [r["pr_number"] for r in result["records"] if r["type"] == "repair"] == [7]
        assert result["reruns"] == []
        assert all(argv[0] == "graphql" for argv in gh.calls)
        assert [k for k in disk(ws)["pr_states"] if ":pr8:" in k] == []
        assert disk(ws)["rerun_attempts"] == {}

    def test_linked_story_from_the_branch_gets_the_linear_comment(
        self, ws: WatcherState, linear: FakeLinearClient
    ) -> None:
        """Catches the story parsed by the client never reaching the Linear comment."""
        gh = FixtureGh(TWO_PAGE_ROUTES, linear_team_keys=("BOD",))

        run_watcher(gh, ws, [REPO], linear=linear, now=T0)

        assert sorted(c["issue_id"] for c in linear.comments) == ["BOD-195", "BOD-286"]

    def test_cancelled_required_actions_job_is_rerun_once_by_post_then_blocked(
        self, ws: WatcherState
    ) -> None:
        """Catches the rerun using the wrong id, verb or endpoint, or repeating."""
        gh = FixtureGh(CANCELLED_ROUTES)

        runs = [run_watcher(gh, ws, [REPO], now=T0 + n * RUN_INTERVAL) for n in range(3)]

        assert [outcomes(r) for r in runs] == [
            [(10, "CANCELLED", "RETRY")],
            [(10, "CANCELLED", "BLOCKED")],
            [(10, "CANCELLED", "BLOCKED")],
        ]
        writes = [argv for argv in gh.calls if argv[0] != "graphql"]
        assert writes == [["--method", "POST", "repos/org/repo/actions/jobs/9201/rerun"]]
        assert disk(ws)["rerun_attempts"] == {f"{REPO}:pr10:{HEAD_10}:proof-full:701": 1}
        assert runs[0]["reruns"] == [
            {
                "rerun": True,
                "result": {"job_id": 9201, "requested": True},
                "repo": REPO,
                "pr_number": 10,
                "check_name": "proof-full",
            }
        ]

    def test_unreadable_repository_is_reported_and_the_other_is_still_handled(
        self, ws: WatcherState
    ) -> None:
        """Catches a real client failure escaping as something the run does not isolate."""
        broken = json.dumps({"errors": [{"message": "Could not resolve to a Repository"}]})

        class TwoRepos(FixtureGh):
            def _call(self, *args: str) -> str:
                if "owner=gone" in args:
                    return broken
                return super()._call(*args)

        gh = TwoRepos(TWO_PAGE_ROUTES)

        result = run_watcher(gh, ws, ["gone/repo", REPO], now=T0)

        assert result["ok"] is False
        assert result["repo_errors"] == [
            {
                "repo": "gone/repo",
                "error": "GitHub GraphQL error: Could not resolve to a Repository",
                "kind": "github_error",
            }
        ]
        assert outcomes(result) == [(7, "CODE_FAILURE", "REPAIR"), (8, "GREEN", "READY")]
        # Failed repo's cursor is not advanced; only the healthy repo's cursor moves.
        assert disk(ws)["merged_cursors"] == {REPO: iso(T0)}
