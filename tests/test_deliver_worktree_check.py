"""Tests for check_worktree_at_commit: the push directory must be at the integration commit."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from verdict.orchestration import deliver_publish
from verdict.orchestration.deliver import DeliverPlan, ReviewEvidence
from verdict.orchestration.deliver_publish import (
    CommandResult,
    PublishError,
    PublishReason,
    check_worktree_at_commit,
)

REASON = PublishReason.WORKTREE_NOT_AT_INTEGRATION_COMMIT
SHA = "a" * 40


def _plan(commit: str = SHA) -> DeliverPlan:
    return DeliverPlan(
        run_id="run-1",
        goal="goal",
        issue_number=7,
        base_branch="main",
        head_branch="verdict/issue-7-add-thing",
        integration_commit=commit,
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


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _commit(repo: Path, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("ignored.txt\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _assert_refused(error: PublishError | None, where: Path | None, commit: str) -> None:
    assert error is not None
    assert error.reason is REASON
    assert error.reason.value == "worktree_not_at_integration_commit"
    assert (str(where) if where is not None else "the current directory") in error.message
    assert f"git merge --ff-only {commit}" in error.message
    assert f"git checkout --detach {commit}" in error.message
    assert "clean" in error.message


def test_at_commit_and_clean_is_accepted(repo: Path) -> None:
    head = _commit(repo, "a.txt")
    assert check_worktree_at_commit(_plan(head), deliver_publish.run_command, repo) is None


def test_other_commit_is_refused(repo: Path) -> None:
    first = _commit(repo, "a.txt")
    _commit(repo, "b.txt")
    _assert_refused(
        check_worktree_at_commit(_plan(first), deliver_publish.run_command, repo), repo, first
    )


def test_modified_tracked_file_is_refused(repo: Path) -> None:
    head = _commit(repo, "a.txt")
    (repo / "a.txt").write_text("changed\n")
    error = check_worktree_at_commit(_plan(head), deliver_publish.run_command, repo)
    _assert_refused(error, repo, head)
    assert error is not None
    assert "a.txt" in error.message


def test_untracked_file_is_refused(repo: Path) -> None:
    head = _commit(repo, "a.txt")
    (repo / "new.txt").write_text("new\n")
    _assert_refused(
        check_worktree_at_commit(_plan(head), deliver_publish.run_command, repo), repo, head
    )


def test_ignored_file_is_not_refused(repo: Path) -> None:
    head = _commit(repo, "a.txt")
    (repo / "ignored.txt").write_text("ignored\n")
    assert check_worktree_at_commit(_plan(head), deliver_publish.run_command, repo) is None


def test_non_repository_directory_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    # Keep git from finding a repository in a parent of tmp_path.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CEILING_DIRECTORIES": str(tmp_path.resolve()),
        "HOME": str(tmp_path),
    }

    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        proc = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, env=env)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    _assert_refused(check_worktree_at_commit(_plan(), runner, plain), plain, SHA)


def test_empty_rev_parse_output_is_refused(tmp_path: Path) -> None:
    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        return CommandResult(0, "  \n")

    _assert_refused(check_worktree_at_commit(_plan(), runner, tmp_path), tmp_path, SHA)


def test_abbreviated_plan_commit_prefixing_head_is_accepted(repo: Path) -> None:
    head = _commit(repo, "a.txt")
    assert check_worktree_at_commit(_plan(head[:12]), deliver_publish.run_command, repo) is None


def test_abbreviated_plan_commit_that_does_not_prefix_head_is_refused(repo: Path) -> None:
    head = _commit(repo, "a.txt")
    other = ("0" if head[0] != "0" else "1") + head[1:12]
    _assert_refused(
        check_worktree_at_commit(_plan(other), deliver_publish.run_command, repo), repo, other
    )


def test_status_is_not_run_when_head_check_fails(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        calls.append(list(argv))
        return CommandResult(0, "b" * 40 + "\n")

    error = check_worktree_at_commit(_plan(), runner, tmp_path)
    _assert_refused(error, tmp_path, SHA)
    assert calls == [["git", "rev-parse", "HEAD"]]


def test_runner_is_called_with_two_positional_arguments(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        calls.append((list(argv), cwd))
        return CommandResult(0, SHA + "\n" if argv[1] == "rev-parse" else "")

    assert check_worktree_at_commit(_plan(), runner, tmp_path) is None
    assert calls == [
        (["git", "rev-parse", "HEAD"], tmp_path),
        (["git", "status", "--porcelain"], tmp_path),
    ]


def test_failing_status_command_is_refused(tmp_path: Path) -> None:
    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        if argv[1] == "rev-parse":
            return CommandResult(0, SHA + "\n")
        return CommandResult(128, "", "fatal: broken index")

    _assert_refused(check_worktree_at_commit(_plan(), runner, tmp_path), tmp_path, SHA)


def test_failing_rev_parse_command_is_refused(tmp_path: Path) -> None:
    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        return CommandResult(128, "", "fatal: not a git repository")

    _assert_refused(check_worktree_at_commit(_plan(), runner, tmp_path), tmp_path, SHA)


def test_message_says_current_directory_when_cwd_is_none() -> None:
    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        return CommandResult(0, "c" * 40 + "\n")

    _assert_refused(check_worktree_at_commit(_plan(), runner), None, SHA)
