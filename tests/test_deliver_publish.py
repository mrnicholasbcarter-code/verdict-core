"""Tests for the deliver_publish git/gh adapter, using a fake recording runner."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from verdict.orchestration import deliver_publish
from verdict.orchestration.deliver import DeliverPlan, ReviewEvidence
from verdict.orchestration.deliver_publish import (
    CommandResult,
    PublishError,
    PublishReason,
    PublishResult,
    publish_branch_and_pr,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
BRANCH = "verdict/issue-7-add-thing"
# The only force-like flag the publisher may send: create-only (the remote ref must not exist).
CREATE_ONLY_LEASE = f"--force-with-lease=refs/heads/{BRANCH}:"
PUSH_ARGV = ["git", "push", CREATE_ONLY_LEASE, "origin", f"{SHA}:refs/heads/{BRANCH}"]
PR_URL = "https://github.com/acme/repo/pull/42"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"


def _plan(head: str = "verdict/issue-7-add-thing", base: str = "main") -> DeliverPlan:
    return DeliverPlan(
        run_id="run-1",
        goal="goal",
        issue_number=7,
        base_branch=base,
        head_branch=head,
        integration_commit=SHA,
        receipt_path=Path("receipt.json"),
        receipt_digest="sha256:r",
        events_digest="sha256:e",
        openspec_change=None,
        nodes=(),
        review=ReviewEvidence(
            status="PASS",
            reviewer="ocr",
            route_id="r",
            blocking=0,
            level="family",
            implementer_families=(),
            reviewer_family="x",
        ),
    )


class FakeRunner:
    def __init__(self, *, handlers: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.handlers = handlers or {}

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        self.calls.append(list(argv))
        for prefix, result in self.handlers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return result
        if argv[:3] == ["gh", "pr", "list"]:
            return CommandResult(0, "[]")
        if argv[:3] == ["gh", "pr", "create"]:
            return CommandResult(0, PR_URL + "\n")
        return CommandResult(0, "")


def _publish(runner: Callable[..., CommandResult], plan: DeliverPlan | None = None):
    return publish_branch_and_pr(plan or _plan(), "Title", "Refs #7", runner=runner)


def test_happy_path_argv_sequence_and_url() -> None:
    runner = FakeRunner()
    result = _publish(runner)
    assert result == PublishResult(pr_url=PR_URL, branch="verdict/issue-7-add-thing", created=True)
    assert not result.reused
    assert runner.calls[0] == ["git", "--version"]
    assert runner.calls[1] == ["gh", "--version"]
    assert runner.calls[2] == ["gh", "auth", "status"]
    assert PUSH_ARGV in runner.calls
    create = runner.calls[-1]
    assert create[:4] == ["gh", "pr", "create", "--draft"]
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == "verdict/issue-7-add-thing"
    assert "--body-file" in create


def _open_pr(**fields: object) -> CommandResult:
    row: dict[str, object] = {"number": 3, "url": PR_URL, "isDraft": True}
    row.update(fields)
    return CommandResult(0, json.dumps([row]))


def test_existing_open_draft_pr_is_reused_without_second_create() -> None:
    runner = FakeRunner(handlers={("gh", "pr", "list"): _open_pr()})
    result = _publish(runner)
    assert isinstance(result, PublishResult)
    assert result.reused and result.pr_url == PR_URL
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
    listing = next(c for c in runner.calls if c[:3] == ["gh", "pr", "list"])
    assert "number,url,isDraft" in listing[listing.index("--json") + 1]


def test_existing_open_pr_that_is_already_ready_is_refused_not_reused() -> None:
    """A ready PR must never be recorded as a draft the gate may leave unchanged."""
    runner = FakeRunner(handlers={("gh", "pr", "list"): _open_pr(isDraft=False)})
    result = _publish(runner)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.PR_NOT_DRAFT
    assert PR_URL in result.message
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)
    assert not any(c[:3] == ["gh", "pr", "ready"] for c in runner.calls)


@pytest.mark.parametrize("is_draft", [None, "true", "false", 1, 0, [], {}])
def test_open_pr_with_unreadable_draft_state_fails_closed(is_draft: object) -> None:
    runner = FakeRunner(handlers={("gh", "pr", "list"): _open_pr(isDraft=is_draft)})
    result = _publish(runner)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.PR_LOOKUP_FAILED
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)


def test_open_pr_without_draft_field_fails_closed() -> None:
    row = json.dumps([{"number": 3, "url": PR_URL}])
    runner = FakeRunner(handlers={("gh", "pr", "list"): CommandResult(0, row)})
    result = _publish(runner)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.PR_LOOKUP_FAILED


def test_remote_branch_at_same_commit_is_not_repushed() -> None:
    line = f"{SHA}\trefs/heads/verdict/issue-7-add-thing\n"
    runner = FakeRunner(handlers={("git", "ls-remote"): CommandResult(0, line)})
    result = _publish(runner)
    assert isinstance(result, PublishResult)
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)


def test_remote_branch_at_other_commit_is_refused() -> None:
    line = f"{OTHER_SHA}\trefs/heads/verdict/issue-7-add-thing\n"
    runner = FakeRunner(handlers={("git", "ls-remote"): CommandResult(0, line)})
    result = _publish(runner)
    assert isinstance(result, PublishError)
    assert result.reason is PublishReason.BRANCH_EXISTS
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)


def test_missing_git() -> None:
    runner = FakeRunner(handlers={("git", "--version"): CommandResult(127, "", "not found")})
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.GIT_MISSING


def test_missing_gh() -> None:
    runner = FakeRunner(handlers={("gh", "--version"): CommandResult(127, "", "not found")})
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.GH_MISSING
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)


def test_unauthenticated_gh() -> None:
    runner = FakeRunner(
        handlers={("gh", "auth", "status"): CommandResult(1, "", f"not logged in token {TOKEN}")}
    )
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.GH_UNAUTHENTICATED
    assert TOKEN not in result.message
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)


def test_push_failure_redacts_secrets() -> None:
    stderr = f"remote: denied https://user:{TOKEN}@github.com/acme/repo.git\ntoken={TOKEN}"
    runner = FakeRunner(handlers={("git", "push"): CommandResult(1, "", stderr)})
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.PUSH_FAILED
    assert TOKEN not in result.message
    assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)


def test_pr_create_failure_redacts_secrets() -> None:
    runner = FakeRunner(
        handlers={("gh", "pr", "create"): CommandResult(1, "", f"GH_TOKEN={TOKEN} boom")}
    )
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.PR_CREATE_FAILED
    assert TOKEN not in result.message


def test_pr_create_without_url_is_an_error() -> None:
    runner = FakeRunner(handlers={("gh", "pr", "create"): CommandResult(0, "created\n")})
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.PR_CREATE_FAILED


def test_pr_lookup_failure_is_distinct() -> None:
    runner = FakeRunner(handlers={("gh", "pr", "list"): CommandResult(1, "", "api down")})
    result = _publish(runner)
    assert isinstance(result, PublishError) and result.reason is PublishReason.PR_LOOKUP_FAILED


def test_reasons_are_distinct() -> None:
    values = [r.value for r in PublishReason]
    assert len(values) == len(set(values))


@pytest.mark.parametrize("head", ["main", "master", "Main"])
def test_head_equal_base_refused_before_any_command(head: str) -> None:
    runner = FakeRunner()
    result = _publish(runner, _plan(head=head, base="main"))
    assert isinstance(result, PublishError) and result.reason is PublishReason.HEAD_IS_BASE
    assert runner.calls == []


def test_custom_base_equal_head_refused() -> None:
    runner = FakeRunner()
    result = _publish(runner, _plan(head="release", base="release"))
    assert isinstance(result, PublishError) and result.reason is PublishReason.HEAD_IS_BASE
    assert runner.calls == []


@pytest.mark.parametrize("head", ["", "-x", "a b", "a..b", "x;rm"])
def test_unsafe_branch_names_refused(head: str) -> None:
    runner = FakeRunner()
    result = _publish(runner, _plan(head=head))
    assert isinstance(result, PublishError)
    assert runner.calls == []


def test_no_force_flags_in_any_argv() -> None:
    runner = FakeRunner()
    _publish(runner)
    assert PUSH_ARGV in runner.calls
    for argv in runner.calls:
        assert "--force" not in argv and "-f" not in argv
        assert "--force-with-lease" not in argv  # never the bare, overwrite-capable form
        assert not any(a.startswith("--force") and a != CREATE_ONLY_LEASE for a in argv)
        assert not any(a.startswith("+") for a in argv)
        assert not any(a.endswith(":refs/heads/main") for a in argv)


def test_default_runner_never_uses_shell() -> None:
    source = inspect.getsource(deliver_publish)
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "timeout=timeout" in source


def test_default_runner_maps_missing_executable() -> None:
    result = deliver_publish.run_command(["verdict-no-such-binary-xyz"])
    assert result.returncode == 127
