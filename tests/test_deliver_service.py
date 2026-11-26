"""Tests for verdict.orchestration.deliver_service, with a fake recording runner."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.deliver_publish import DEFAULT_PUSH_TIMEOUT_S, CommandResult
from verdict.orchestration.deliver_service import (
    DELIVERY_DIGEST_FILE,
    DELIVERY_FILE,
    DeliverOutcome,
    DeliverRunRequest,
    DeliverStatus,
    deliver_run,
    read_delivery_record,
    write_delivery_record,
)
from verdict.orchestration.receipt import EventLog, write_run_receipt

VERIFY = ("pytest", "-q", "tests/test_a.py")
INTEGRATION = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
PASS = {"status": "PASS", "reviewer": "ocr v1", "route_id": "gm/gemini-3", "findings": []}
PR_URL = "https://github.com/acme/repo/pull/77"
FIXED = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
BRANCH = "verdict/issue-1269-deliver-it"


def _make_run(
    tmp_path: Path, *, outcome: str = "COMPLETE", review: dict[str, Any] | None = PASS
) -> Path:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    graph = WorkGraph(
        goal="add feature x",
        nodes=(WorkNode("a", "build a", owned_files=("pkg/a.py",), verification_command=VERIFY),),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl")
    log.emit("run_started", run_id="run-1", goal="add feature x")
    log.emit("dispatch", node_id="a", attempt=1, route_id="cc/claude-sonnet-5", capacity_class="x")
    log.emit("terminal", node_id="a", attempt=1, ok=True, duration_seconds=2.0)
    log.emit("verify", node_id="a", ok=True, command=list(VERIFY), exit_code=0)
    log.emit("integrate", ok=True, commits=[INTEGRATION], ref=INTEGRATION, commit=INTEGRATION)
    log.emit("barrier", name="integration", ok=True)
    log.emit("controller", state="REVIEW_INDEPENDENCE", level="family", reviewer_route="x")
    if review is not None:
        (run_dir / "review.json").write_text(json.dumps(review))
    log.emit("run_finished", outcome=outcome, reason="done", integration_ref=INTEGRATION)
    write_run_receipt(run_dir)
    return run_dir


class FakeRunner:
    def __init__(self, handlers: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        #: the ``timeout`` each call was made with (``None``: the runner's own default),
        #: aligned with ``calls``; the real ``run_command`` takes the same keyword
        self.timeouts: list[float | None] = []
        self.handlers = handlers or {}

    def __call__(
        self, argv: Sequence[str], cwd: Path | None, *, timeout: float | None = None
    ) -> CommandResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        for prefix, result in self.handlers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return result
        if argv[:3] == ["gh", "pr", "list"]:
            return CommandResult(0, "[]")
        if argv[:3] == ["gh", "pr", "create"]:
            return CommandResult(0, PR_URL + "\n")
        return CommandResult(0, "")

    @property
    def mutating(self) -> list[list[str]]:
        return [
            c for c in self.calls if c[:2] == ["git", "push"] or c[:3] == ["gh", "pr", "create"]
        ]


def _request(run_dir: Path, **kw: Any) -> DeliverRunRequest:
    return DeliverRunRequest(run_dir=run_dir, issue_number=1269, issue_title="Deliver it!", **kw)


def _clock() -> datetime:
    return FIXED


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ineligible_run_is_refused_without_commands(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, outcome="BLOCKED")
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.REFUSED
    assert outcome.reason == "run_not_complete"
    assert not outcome.ok
    assert runner.calls == []
    assert not (run_dir / DELIVERY_FILE).exists()


def test_failed_review_is_refused_with_gate_reason(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, review=None)
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.REFUSED
    assert outcome.reason == "review_failed"
    assert runner.calls == []


def test_dry_run_returns_rendered_plan_and_runs_nothing(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir, dry_run=True), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DRY_RUN
    assert outcome.ok
    assert outcome.branch == BRANCH
    assert outcome.base_branch == "main"
    assert outcome.title.startswith("verdict: ")
    assert "Refs #1269" in outcome.body
    assert outcome.pr_url == ""
    assert runner.mutating == []
    assert runner.calls == []
    assert not (run_dir / DELIVERY_FILE).exists()


def test_successful_deliver_returns_url_and_writes_record(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.DELIVERED
    assert outcome.pr_url == PR_URL
    assert outcome.branch == BRANCH
    assert outcome.created
    assert outcome.delivered_at == "2026-11-26T12:00:00Z"
    assert len(runner.mutating) == 2
    lease = f"--force-with-lease=refs/heads/{BRANCH}:"  # create-only: the branch must not exist
    assert ["git", "push", lease, "origin", f"{INTEGRATION}:refs/heads/{BRANCH}"] in runner.calls

    record = read_delivery_record(run_dir)
    assert record is not None
    assert record.pr_url == PR_URL
    assert record.branch == BRANCH
    assert record.delivered_at == "2026-11-26T12:00:00Z"
    assert record.run_id == "run-1"
    assert record.issue_number == 1269
    assert outcome.record_path == run_dir / DELIVERY_FILE
    digest = (run_dir / DELIVERY_DIGEST_FILE).read_text().strip()
    assert digest == "sha256:" + _sha(run_dir / DELIVERY_FILE)


def test_second_deliver_is_idempotent(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    first = deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    second_runner = FakeRunner()
    second = deliver_run(
        _request(run_dir),
        runner=second_runner,
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert second.status is DeliverStatus.ALREADY_DELIVERED
    assert second.ok
    assert second.pr_url == first.pr_url == PR_URL
    assert second.delivered_at == first.delivered_at
    assert second_runner.calls == []


def test_publisher_error_fails_without_record(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner(
        handlers={("git", "push"): CommandResult(1, "", "remote: rejected by vgate")}
    )
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert outcome.reason == "push_failed"
    assert not outcome.ok
    assert "vgate" in outcome.message
    assert not (run_dir / DELIVERY_FILE).exists()
    assert not (run_dir / DELIVERY_DIGEST_FILE).exists()


def test_unauthenticated_gh_fails_with_publisher_reason(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner(handlers={("gh", "auth", "status"): CommandResult(1, "", "not logged in")})
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert outcome.reason == "gh_unauthenticated"
    assert runner.mutating == []
    assert not (run_dir / DELIVERY_FILE).exists()


def test_failed_deliver_can_be_retried(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    bad = FakeRunner(handlers={("gh", "pr", "create"): CommandResult(1, "", "boom")})
    assert deliver_run(_request(run_dir), runner=bad, clock=_clock).status is DeliverStatus.FAILED
    good = deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    assert good.status is DeliverStatus.DELIVERED


def test_receipt_and_events_are_byte_identical(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    before = {n: (run_dir / n).read_bytes() for n in ("events.jsonl", "receipt.json")}
    deliver_run(_request(run_dir, dry_run=True), runner=FakeRunner(), clock=_clock)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    after = {n: (run_dir / n).read_bytes() for n in before}
    assert before == after


def test_delivered_run_still_verifies_receipt(tmp_path: Path) -> None:
    from verdict.orchestration.receipt import verify_run_receipt

    run_dir = _make_run(tmp_path)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    assert verify_run_receipt(run_dir, repair=False) == []


def test_tampered_delivery_record_fails_closed(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    path = run_dir / DELIVERY_FILE
    path.write_text(path.read_text().replace(PR_URL, "https://example.com/pull/1"))
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert outcome.reason == "delivery_record_invalid"
    assert runner.calls == []


def test_record_for_another_issue_is_a_mismatch(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    runner = FakeRunner()
    other = DeliverRunRequest(run_dir=run_dir, issue_number=5, issue_title="x")
    outcome = deliver_run(other, runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert outcome.reason == "delivery_record_mismatch"
    assert outcome.pr_url == PR_URL
    assert runner.calls == []


def test_record_write_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from verdict.orchestration import deliver_service

    def boom(run_dir: Path, record: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(deliver_service, "write_delivery_record", boom)
    run_dir = _make_run(tmp_path)
    outcome = deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert outcome.reason == "record_write_failed"
    assert outcome.pr_url == PR_URL


def test_to_dict_is_json_serializable_for_every_status(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    outcomes: list[DeliverOutcome] = [
        deliver_run(_request(run_dir, dry_run=True), runner=FakeRunner(), clock=_clock),
        deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock),
        deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock),
        deliver_run(_request(tmp_path / "missing"), runner=FakeRunner(), clock=_clock),
    ]
    statuses = [json.loads(json.dumps(o.to_dict()))["status"] for o in outcomes]
    assert statuses == ["dry_run", "delivered", "already_delivered", "refused"]
    delivered = outcomes[1].to_dict()
    assert delivered["pr_url"] == PR_URL
    assert delivered["record_path"] == str(run_dir / DELIVERY_FILE)
    assert delivered["ok"] is True


def test_default_runner_is_used_when_none_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.orchestration import deliver_service

    seen = FakeRunner()
    monkeypatch.setattr(deliver_service, "run_command", seen)
    run_dir = _make_run(tmp_path)
    outcome = deliver_run(_request(run_dir), clock=_clock)
    assert outcome.status is DeliverStatus.DELIVERED
    assert seen.calls
    # only the push carries the push timeout; every other command keeps the runner's default
    pushes = [t for c, t in zip(seen.calls, seen.timeouts, strict=True) if c[:2] == ["git", "push"]]
    assert pushes == [DEFAULT_PUSH_TIMEOUT_S]
    assert all(
        t is None
        for c, t in zip(seen.calls, seen.timeouts, strict=True)
        if c[:2] != ["git", "push"]
    )


# --- F10: a stored record is bound to this run, not only to the issue number --------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "run-from-somewhere-else"),
        ("integration_commit", "f" * 40),
        ("receipt_digest", "sha256:" + "0" * 64),
        ("events_digest", "sha256:" + "1" * 64),
        ("receipt_digest", ""),  # an old or hand-made record that carries no digest
    ],
)
def test_record_copied_from_another_run_of_the_same_issue_is_a_mismatch(
    tmp_path: Path, field: str, value: str
) -> None:
    run_dir = _make_run(tmp_path)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    record = read_delivery_record(run_dir)
    assert record is not None
    # a perfectly valid json + digest pair, for the right issue, but not for this run
    write_delivery_record(run_dir, replace(record, **{field: value}))
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert not outcome.ok
    assert outcome.reason == "delivery_record_mismatch"
    assert field in outcome.message
    assert outcome.pr_url == PR_URL
    assert runner.calls == []


def test_record_that_matches_this_run_is_still_already_delivered(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    deliver_run(_request(run_dir), runner=FakeRunner(), clock=_clock)
    runner = FakeRunner()
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.ALREADY_DELIVERED
    assert runner.calls == []


# --- F1: the library entry refuses a deadline that is not a finite positive number ---------


@pytest.mark.parametrize("deadline", [math.nan, math.inf, -math.inf, 0.0, -5.0])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unbounded_or_zero_ci_deadline_is_refused_before_any_command(
    tmp_path: Path, deadline: float, dry_run: bool
) -> None:
    run_dir = _make_run(tmp_path)
    runner = FakeRunner()
    outcome = deliver_run(
        _request(run_dir, ci_deadline_s=deadline, dry_run=dry_run), runner=runner, clock=_clock
    )
    assert outcome.status is DeliverStatus.REFUSED
    assert outcome.reason == "invalid_request"
    assert "deadline" in outcome.message
    assert runner.calls == []
    assert not (run_dir / DELIVERY_FILE).exists()


# --- F4: an open pull request that is already ready is never reused as a draft -------------


def test_open_pr_that_is_already_ready_fails_with_pr_not_draft_and_is_not_recorded(
    tmp_path: Path,
) -> None:
    run_dir = _make_run(tmp_path)
    ready_pr = json.dumps([{"number": 3, "url": PR_URL, "isDraft": False}])
    runner = FakeRunner(handlers={("gh", "pr", "list"): CommandResult(0, ready_pr)})
    outcome = deliver_run(_request(run_dir), runner=runner, clock=_clock)
    assert outcome.status is DeliverStatus.FAILED
    assert outcome.reason == "pr_not_draft"
    assert outcome.ready is False
    assert not (run_dir / DELIVERY_FILE).exists()  # nothing claims ready=False for a ready PR
    assert not (run_dir / DELIVERY_DIGEST_FILE).exists()
    assert not any(c[:3] == ["gh", "pr", "ready"] or c[:2] == ["gh", "api"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
