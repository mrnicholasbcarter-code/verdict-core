"""Tests for the ready gate wiring in ``deliver_run`` and ``verdict deliver``.

git, ``gh``, the reviewer, the clock and the gate's sleep are fakes. Nothing live
runs and no real home directory is touched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.test_deliver_service import INTEGRATION, PR_URL, FakeRunner, _clock, _make_run, _request
from verdict import cli
from verdict.orchestration import deliver_service, ready_gate
from verdict.orchestration.contracts import ReviewResult
from verdict.orchestration.deliver_publish import CommandResult
from verdict.orchestration.deliver_service import (
    DELIVERY_DIGEST_FILE,
    DELIVERY_FILE,
    DeliverOutcome,
    DeliverStatus,
    deliver_run,
    read_delivery_record,
)
from verdict.orchestration.ready_gate import CrossFamilyReviewRequest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))


class GhRunner(FakeRunner):
    """``FakeRunner`` that also answers the ready gate's ``gh`` calls."""

    def __init__(self, *, checks: str = "success", head: str = INTEGRATION) -> None:
        super().__init__()
        self.checks = checks
        self.head = head

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        call = list(argv)
        if call[:3] == ["gh", "pr", "view"] and call[3:4] == [PR_URL]:
            self.calls.append(call)
            return CommandResult(0, json.dumps({"headRefOid": self.head}))
        if call[:2] == ["gh", "api"] and call[2].endswith("/status"):
            self.calls.append(call)
            return CommandResult(0, json.dumps({"state": "success", "total_count": 0}))
        if call[:2] == ["gh", "api"] and "/check-runs" in call[2]:
            self.calls.append(call)
            run = {"name": "ci", "status": "completed", "conclusion": self.checks}
            return CommandResult(0, json.dumps({"total_count": 1, "check_runs": [run]}))
        return super().__call__(argv, cwd)

    def ready_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "pr", "ready"]]


class FakeReviewer:
    def __init__(self) -> None:
        self.requests: list[CrossFamilyReviewRequest] = []

    def __call__(self, request: CrossFamilyReviewRequest) -> ReviewResult:
        self.requests.append(request)
        return ReviewResult("PASS", "ocr", "gm/gemini-3")


def _spy_gate(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real = deliver_service.run_pr_ready_gate

    def spy(plan: Any, **kwargs: Any) -> Any:
        calls.append({"plan": plan, **kwargs})
        return real(plan, **kwargs)

    monkeypatch.setattr(deliver_service, "run_pr_ready_gate", spy)
    return calls


def test_gate_is_called_after_publish_with_the_right_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    runner = GhRunner()
    reviewer = FakeReviewer()
    calls = _spy_gate(monkeypatch)
    repo = tmp_path / "repo"
    deliver_run(
        _request(run_dir, ci_deadline_s=90.0, repo_dir=repo),
        runner=runner,
        clock=_clock,
        reviewer=reviewer,
    )
    assert len(calls) == 1
    call = calls[0]
    assert call["pr_ref"] == PR_URL
    assert call["head_sha"] == INTEGRATION
    assert call["reviewer"] is reviewer
    assert call["runner"] is runner
    assert call["cwd"] == repo
    assert call["deadline_s"] == 90.0
    assert call["plan"].integration_commit == INTEGRATION
    create = next(i for i, c in enumerate(runner.calls) if c[:3] == ["gh", "pr", "create"])
    view = next(i for i, c in enumerate(runner.calls) if c[:3] == ["gh", "pr", "view"])
    assert create < view


def test_default_deadline_and_no_reviewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path)
    calls = _spy_gate(monkeypatch)
    deliver_run(_request(run_dir), runner=GhRunner(), clock=_clock)
    assert calls[0]["deadline_s"] == ready_gate.DEFAULT_CI_DEADLINE_S
    assert calls[0]["reviewer"] is None
    assert _request(run_dir).ci_deadline_s == ready_gate.DEFAULT_CI_DEADLINE_S


def test_ready_success_is_recorded(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = GhRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DELIVERED
    assert outcome.ready is True
    assert outcome.ready_reason == ""
    assert outcome.review_level == "family"
    assert outcome.reviewer_route == "gm/gemini-3"
    assert outcome.extra_review_ran is False
    assert outcome.ci_polls == 1
    assert len(runner.ready_calls()) == 1
    raw = json.loads((run_dir / DELIVERY_FILE).read_text())
    assert raw["ready"] is True
    assert raw["ready_reason"] == ""
    assert raw["review_level"] == "family"
    assert raw["reviewer_route"] == "gm/gemini-3"
    assert raw["extra_review_ran"] is False
    assert raw["ci_polls"] == 1
    record = read_delivery_record(run_dir)
    assert record is not None and record.ready is True and record.ci_polls == 1
    data = outcome.to_dict()
    assert data["ready"] is True and data["ci_polls"] == 1


def test_gate_stop_keeps_delivered_and_draft(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = GhRunner(checks="failure")
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DELIVERED
    assert outcome.ok is True
    assert outcome.ready is False
    assert outcome.ready_reason == "ci_red"
    assert outcome.pr_url == PR_URL
    assert runner.ready_calls() == []
    assert not any(c[:3] == ["gh", "pr", "edit"] for c in runner.calls)
    record = read_delivery_record(run_dir)
    assert record is not None and record.ready is False and record.ready_reason == "ci_red"


def test_head_moved_stop_is_recorded(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = GhRunner(head="f" * 40)
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DELIVERED
    assert outcome.ready_reason == "head_moved"
    assert runner.ready_calls() == []


def test_no_ready_skips_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path)
    runner = GhRunner()
    calls = _spy_gate(monkeypatch)
    outcome = deliver_run(_request(run_dir, run_ready_gate=False), runner=runner, clock=_clock)
    assert calls == []
    assert outcome.status is DeliverStatus.DELIVERED
    assert outcome.ready is False
    assert outcome.ready_reason == "skipped"
    assert runner.ready_calls() == []
    assert not any(c[:2] == ["gh", "api"] for c in runner.calls)
    record = read_delivery_record(run_dir)
    assert record is not None and record.ready_reason == "skipped" and record.ready is False


def test_dry_run_never_calls_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path)
    runner = GhRunner()
    calls = _spy_gate(monkeypatch)
    outcome = deliver_run(_request(run_dir, dry_run=True), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DRY_RUN
    assert outcome.ready is False and outcome.ready_reason == ""
    assert calls == []
    assert runner.calls == []


def test_already_delivered_never_calls_the_gate_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path)
    first = deliver_run(_request(run_dir), runner=GhRunner(), clock=_clock)
    assert first.ready is True
    calls = _spy_gate(monkeypatch)
    runner = GhRunner()
    second = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert second.status is DeliverStatus.ALREADY_DELIVERED
    assert second.ready is True
    assert second.review_level == "family"
    assert calls == []
    assert runner.calls == []


def test_refused_run_never_calls_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, outcome="BLOCKED")
    calls = _spy_gate(monkeypatch)
    outcome = deliver_run(_request(run_dir), runner=GhRunner(), clock=_clock)
    assert outcome.status is DeliverStatus.REFUSED
    assert calls == []


def test_old_record_without_ready_fields_reads_with_defaults(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    deliver_run(_request(run_dir, run_ready_gate=False), runner=GhRunner(), clock=_clock)
    raw = json.loads((run_dir / DELIVERY_FILE).read_text())
    for key in (
        "ready",
        "ready_reason",
        "review_level",
        "reviewer_route",
        "extra_review_ran",
        "ci_polls",
    ):
        del raw[key]
    data = (json.dumps(raw, indent=2, sort_keys=True) + "\n").encode()
    (run_dir / DELIVERY_FILE).write_bytes(data)
    (run_dir / DELIVERY_DIGEST_FILE).write_text(deliver_service._digest(data) + "\n")
    record = read_delivery_record(run_dir)
    assert record is not None
    assert record.ready is False
    assert record.ready_reason == ""
    assert record.review_level == ""
    assert record.reviewer_route == ""
    assert record.extra_review_ran is False
    assert record.ci_polls == 0
    again = deliver_run(_request(run_dir), runner=GhRunner(), clock=_clock)
    assert again.status is DeliverStatus.ALREADY_DELIVERED


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> Any:
    seen: list[Any] = []

    def fake(request: Any, **_kw: Any) -> DeliverOutcome:
        seen.append(request)
        return DeliverOutcome(status=DeliverStatus.DELIVERED, pr_url=PR_URL)

    monkeypatch.setattr(deliver_service, "deliver_run", fake)
    monkeypatch.setattr(
        cli.sys, "argv", ["verdict", "deliver", str(tmp_path), "--issue", "9", *argv]
    )
    cli.main()
    return seen[0]


def test_cli_defaults_reach_the_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _cli(monkeypatch, tmp_path)
    assert request.ci_deadline_s == 3600.0
    # one number, two entry points: the CLI default (minutes) and the library default (seconds)
    assert request.ci_deadline_s == ready_gate.DEFAULT_CI_DEADLINE_S
    assert request.run_ready_gate is True


def test_cli_options_reach_the_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _cli(monkeypatch, tmp_path, "--ci-deadline", "5", "--no-ready")
    assert request.ci_deadline_s == 300.0
    assert request.run_ready_gate is False


# --- F9: the recorded independence level is checked against the reviewer's real family -----

SAME_FAMILY_REVIEW = {
    "status": "PASS",
    "reviewer": "ocr v1",
    "route_id": "cc/claude-opus-5",
    "findings": [],
}


def test_same_family_reviewer_with_a_recorded_family_level_still_gets_the_extra_review(
    tmp_path: Path,
) -> None:
    run_dir = _make_run(tmp_path, review=SAME_FAMILY_REVIEW)  # the run recorded "family"
    runner, reviewer = GhRunner(), FakeReviewer()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock, reviewer=reviewer)
    assert outcome.review_level == "route"
    assert outcome.extra_review_ran is True
    assert len(reviewer.requests) == 1
    assert reviewer.requests[0].exclude_families == frozenset({"claude"})
    assert outcome.ready is True


def test_same_family_reviewer_without_an_extra_reviewer_stays_a_draft(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, review=SAME_FAMILY_REVIEW)
    runner = GhRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DELIVERED
    assert outcome.ready is False
    assert outcome.ready_reason == "no_cross_family_reviewer"
    assert runner.ready_calls() == []


def test_empty_reviewer_route_is_refused_before_any_command(tmp_path: Path) -> None:
    review = {"status": "PASS", "reviewer": "ocr v1", "route_id": "", "findings": []}
    run_dir = _make_run(tmp_path, review=review)
    runner = GhRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.REFUSED
    assert outcome.reason == "review_failed"
    assert runner.calls == []
