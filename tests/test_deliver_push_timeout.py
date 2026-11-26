"""Timeout handling of the ``verdict deliver`` command runner and the push call."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.test_deliver_publish import _plan
from verdict.orchestration import deliver_publish
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
