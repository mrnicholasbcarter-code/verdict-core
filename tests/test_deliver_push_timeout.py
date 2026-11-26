"""Timeout handling of the ``verdict deliver`` command runner and the push call."""

from __future__ import annotations

import contextlib
import functools
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.test_deliver_publish import _plan
from verdict.orchestration import deliver_publish
from verdict.orchestration.deliver import DeliverPlan
from verdict.orchestration.deliver_publish import (
    DEFAULT_PUSH_TIMEOUT_S,
    CommandResult,
    PublishError,
    PublishReason,
    publish_branch_and_pr,
    push_branch,
    run_command,
)

_SPAWNER = textwrap.dedent(
    """
    import os
    import subprocess
    import sys
    import time

    out = sys.argv[1]
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(out + ".tmp", "w") as handle:
        handle.write(f"{os.getpid()} {child.pid}")
    os.replace(out + ".tmp", out)
    time.sleep(60)
    """
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _wait_dead(pid: int, limit: float = 5.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def test_timeout_ends_whole_process_group(tmp_path: Path) -> None:
    pids_file = tmp_path / "pids"
    script = tmp_path / "spawner.py"
    script.write_text(_SPAWNER, encoding="utf-8")
    result = run_command([sys.executable, str(script), str(pids_file)], tmp_path, timeout=1.0)
    assert result == CommandResult(124, "", "command timed out after 1s")
    parent, grandchild = (int(part) for part in pids_file.read_text().split())
    assert _wait_dead(parent)
    assert _wait_dead(grandchild)


def test_interrupt_ends_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C during a command must not leave it running: it is in its own session now."""
    pids_file = tmp_path / "pids"
    script = tmp_path / "spawner.py"
    script.write_text(_SPAWNER, encoding="utf-8")
    real_communicate = subprocess.Popen.communicate
    interrupted: list[bool] = []

    def interrupting(self: subprocess.Popen[str], *args: object, **kwargs: object) -> object:
        if not interrupted:
            interrupted.append(True)
            deadline = time.monotonic() + 10.0
            while not pids_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupting)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_command([sys.executable, str(script), str(pids_file)], tmp_path)
        assert pids_file.exists()
        parent, grandchild = (int(part) for part in pids_file.read_text().split())
        assert _wait_dead(parent)
        assert _wait_dead(grandchild)
    finally:
        if pids_file.exists():
            for part in pids_file.read_text().split():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(part), signal.SIGKILL)


def test_run_command_returns_output_and_code(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = run_command([sys.executable, "-c", code], tmp_path)
    assert result == CommandResult(3, "out\n", "err\n")


def test_run_command_missing_executable(tmp_path: Path) -> None:
    result = run_command([str(tmp_path / "no-such-binary")], tmp_path)
    assert result.returncode == 127


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        self.calls.append(list(argv))
        if argv[:2] == ["git", "ls-remote"]:
            return CommandResult(0, "")
        if argv[:3] == ["gh", "pr", "list"]:
            return CommandResult(0, "[]")
        if argv[:3] == ["gh", "pr", "create"]:
            return CommandResult(0, "https://github.com/o/r/pull/9\n")
        return CommandResult(0, "")


def test_push_runner_gets_only_the_push() -> None:
    runner = _Recorder()
    push_runner = _Recorder()
    result = publish_branch_and_pr(_plan(), "title", "body", runner=runner, push_runner=push_runner)
    assert not isinstance(result, PublishError)
    assert len(push_runner.calls) == 1
    assert push_runner.calls[0][:2] == ["git", "push"]
    assert all(call[:2] != ["git", "push"] for call in runner.calls)
    assert runner.calls


def test_injected_runner_also_runs_the_push() -> None:
    runner = _Recorder()
    result = publish_branch_and_pr(_plan(), "title", "body", runner=runner)
    assert not isinstance(result, PublishError)
    assert sum(call[:2] == ["git", "push"] for call in runner.calls) == 1


def test_default_runner_pushes_with_push_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], float | None]] = []

    def fake(
        argv: Sequence[str], cwd: Path | None = None, *, timeout: float = 120.0
    ) -> CommandResult:
        seen.append((list(argv), timeout))
        if argv[:2] == ["git", "ls-remote"]:
            return CommandResult(0, "")
        if argv[:3] == ["gh", "pr", "list"]:
            return CommandResult(0, "[]")
        if argv[:3] == ["gh", "pr", "create"]:
            return CommandResult(0, "https://github.com/o/r/pull/9\n")
        return CommandResult(0, "")

    monkeypatch.setattr(deliver_publish, "run_command", fake)
    result = publish_branch_and_pr(_plan(), "title", "body", runner=fake)
    assert not isinstance(result, PublishError)
    push_timeouts = [t for argv, t in seen if argv[:2] == ["git", "push"]]
    assert push_timeouts == [DEFAULT_PUSH_TIMEOUT_S]
    assert all(t == 120.0 for argv, t in seen if argv[:2] != ["git", "push"])
    assert DEFAULT_PUSH_TIMEOUT_S == 1800.0


def test_push_timeout_is_push_failed_and_names_option() -> None:
    def runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        if argv[:2] == ["git", "push"]:
            return CommandResult(124, "", "command timed out after 1800s")
        return CommandResult(0, "")

    error = push_branch(_plan(), runner)
    assert isinstance(error, PublishError)
    assert error.reason is PublishReason.PUSH_FAILED
    assert "timed out after 1800 seconds" in error.message
    assert "--push-timeout" in error.message


# --- a real push through a slow pre-push hook ---------------------------------------------

BRANCH = "verdict/issue-7-add-thing"


class _Repo(NamedTuple):
    work: Path
    remote: Path
    hooks: Path


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=60
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Repo:
    """A clone with a bare local remote; its hooks live in ``core.hooksPath`` under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("VERDICT_PREPUSH_SKIP", raising=False)
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    hooks = tmp_path / "hooks"
    work.mkdir()
    hooks.mkdir()
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "core.hooksPath", str(hooks))
    _git(work, "remote", "add", "origin", str(remote))
    (work / "one").write_text("one", encoding="utf-8")
    _git(work, "add", "one")
    _git(work, "commit", "-q", "-m", "one")
    return _Repo(work, remote, hooks)


def _install_hook(repo: _Repo, body: str) -> None:
    hook = repo.hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    hook.chmod(0o755)


def _head_plan(repo: _Repo) -> DeliverPlan:
    return replace(_plan(head=BRANCH), integration_commit=_git(repo.work, "rev-parse", "HEAD"))


def _remote_branch(repo: _Repo) -> str:
    return _git(repo.remote, "for-each-ref", "--format=%(objectname)", f"refs/heads/{BRANCH}")


def test_push_through_a_hook_slower_than_the_command_timeout_succeeds_under_the_push_timeout(
    repo: _Repo, tmp_path: Path
) -> None:
    ran = tmp_path / "hook-ran"
    _install_hook(repo, f"sleep 3\ntouch {shlex.quote(str(ran))}")
    plan = _head_plan(repo)
    ordinary = functools.partial(run_command, timeout=1.0)
    push = functools.partial(run_command, timeout=60.0)
    started = time.monotonic()
    error = push_branch(plan, ordinary, repo.work, push_runner=push)
    assert error is None
    assert time.monotonic() - started >= 3.0
    assert ran.exists()
    assert _remote_branch(repo) == plan.integration_commit


def test_the_same_slow_hook_fails_when_the_push_only_has_the_command_timeout(
    repo: _Repo, tmp_path: Path
) -> None:
    """Control for the test above: without a push timeout of its own the push is cut off."""
    _install_hook(repo, "sleep 3")
    ordinary = functools.partial(run_command, timeout=1.0)
    error = push_branch(_head_plan(repo), ordinary, repo.work)
    assert isinstance(error, PublishError)
    assert error.reason is PublishReason.PUSH_FAILED
    assert "timed out after 1 seconds" in error.message
    assert _remote_branch(repo) == ""


@pytest.mark.parametrize("ignore_sigterm", [False, True], ids=["sigterm", "sigkill-escalation"])
def test_a_hook_that_outlives_the_push_timeout_is_push_failed_and_nothing_survives(
    repo: _Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignore_sigterm: bool
) -> None:
    monkeypatch.setattr(deliver_publish, "_KILL_GRACE_S", 0.5)
    pids = tmp_path / "hook-pids"
    quoted = shlex.quote(str(pids))
    trap = "trap '' TERM\n" if ignore_sigterm else ""
    _install_hook(
        repo, f'{trap}sleep 60 &\necho "$$ $!" > {quoted}.tmp\nmv {quoted}.tmp {quoted}\nwait'
    )
    plan = _head_plan(repo)
    ordinary = functools.partial(run_command, timeout=60.0)
    push = functools.partial(run_command, timeout=1.0)
    try:
        error = push_branch(plan, ordinary, repo.work, push_runner=push)
        assert isinstance(error, PublishError)
        assert error.reason is PublishReason.PUSH_FAILED
        assert "timed out after 1 seconds" in error.message
        assert "--push-timeout" in error.message
        assert pids.exists(), "the pre-push hook never started"
        hook_pid, child_pid = (int(part) for part in pids.read_text().split())
        assert _wait_dead(hook_pid)
        assert _wait_dead(child_pid)
        assert _remote_branch(repo) == ""
    finally:
        if pids.exists():
            for part in pids.read_text().split():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(part), signal.SIGKILL)
