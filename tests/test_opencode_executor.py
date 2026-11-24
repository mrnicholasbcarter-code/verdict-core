"""Tests for OpenCodeExecutor and helpers (no live model calls; fake binary).

All subprocess calls go to a tiny Python script written to tmp_path.  The
real opencode binary is never invoked.  Config env vars are overridden so the
real ~/.opencode or ~/.config/opencode are never read or written.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from verdict.orchestration.executor_opencode import (
    CONTEXT_FILE_NAME,
    PINNED_VERSION,
    OpenCodeExecutor,
    _attempt_session_id,
    _build_child_env,
    _kill_process_group,
    _opencode_binary,
    _parse_ndjson_output,
    _write_attempt_config,
    _write_context_file,
)

ROUTE = "cc/claude-sonnet-5"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_opencode(tmp_path: Path, body: str) -> str:
    """Write an executable fake opencode Python script; return its path."""
    script = tmp_path / "fake-opencode"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys, json, os, time\n" + textwrap.dedent(body)
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def run_executor(
    executor: OpenCodeExecutor, prompt: str, cwd: Path, timeout: float = 10.0
) -> object:
    return asyncio.run(executor.run(prompt, route_id=ROUTE, cwd=cwd, timeout_seconds=timeout))


def ndjson_success(text: str = "Done", session_id: str = "ses_abc123") -> str:
    """Build a minimal NDJSON success payload (two lines)."""
    lines = [
        json.dumps(
            {
                "type": "step_finish",
                "sessionID": session_id,
                "reason": "stop",
                "tokens": {"input": 100, "output": 42},
                "timestamp": "2026-11-23T00:00:00Z",
            }
        ),
        json.dumps(
            {
                "type": "text",
                "sessionID": session_id,
                "text": text,
                "timestamp": "2026-11-23T00:00:00Z",
            }
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _opencode_binary
# ---------------------------------------------------------------------------


def test_opencode_binary_uses_env_override(tmp_path: Path) -> None:
    custom = str(tmp_path / "my-opencode")
    env = {"VERDICT_OPENCODE_BIN": custom, "HOME": str(tmp_path)}
    assert _opencode_binary(env) == custom


def test_opencode_binary_prefers_local_install(tmp_path: Path) -> None:
    local_bin = tmp_path / ".opencode" / "bin" / "opencode"
    local_bin.parent.mkdir(parents=True)
    local_bin.touch()
    env = {"HOME": str(tmp_path)}
    assert _opencode_binary(env) == str(local_bin)


def test_opencode_binary_does_not_read_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """_opencode_binary with an explicit env dict does not touch os.environ."""
    real_home = str(Path.home())
    env = {"HOME": "/nonexistent-home-xyz", "PATH": ""}
    result = _opencode_binary(env)
    assert real_home not in result


# ---------------------------------------------------------------------------
# _build_child_env
# ---------------------------------------------------------------------------


def test_build_child_env_sets_isolation_vars(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    env = _build_child_env(
        config_dir=config_dir,
        data_dir=data_dir,
        state_dir=state_dir,
        api_key_env="VERDICT_WORKER_API_KEY",
        base_env={},
    )
    assert env["OPENCODE_CONFIG_DIR"] == str(config_dir)
    assert env["XDG_DATA_HOME"] == str(data_dir)
    assert env["XDG_STATE_HOME"] == str(state_dir)
    assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"


def test_build_child_env_allowlist_blocks_provider_credentials(tmp_path: Path) -> None:
    """Allowlist approach: provider credentials must never reach the child."""
    base = {
        "OMNIROUTE_API_KEY": "secret-omni",
        "OPENAI_API_KEY": "sk-secret",
        "ANTHROPIC_API_KEY": "ant-secret",
        "ANTHROPIC_AUTH_TOKEN": "ant-token",
        "OPENROUTER_API_KEY": "or-secret",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "GITHUB_TOKEN": "gh-token",
        "VERDICT_WORKER_API_KEY": "worker-key",
        "HOME": str(tmp_path),
    }
    env = _build_child_env(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        api_key_env="VERDICT_WORKER_API_KEY",
        base_env=base,
    )
    blocked = [
        "OMNIROUTE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENROUTER_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
    ]
    for key in blocked:
        assert key not in env, f"{key} must not reach the child process"
    # Worker key is preserved (the config references it via {env:...})
    assert env["VERDICT_WORKER_API_KEY"] == "worker-key"
    assert env["HOME"] == str(tmp_path)


def test_build_child_env_sets_disable_claude_code(tmp_path: Path) -> None:
    """OPENCODE_DISABLE_CLAUDE_CODE must be set so ~/.claude is not loaded."""
    env = _build_child_env(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        api_key_env="VERDICT_WORKER_API_KEY",
        base_env={},
    )
    assert env.get("OPENCODE_DISABLE_CLAUDE_CODE") == "1"


# ---------------------------------------------------------------------------
# _write_attempt_config
# ---------------------------------------------------------------------------


def test_write_attempt_config_no_plaintext_key(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_attempt_config(
        tmp_path / "cfg",
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    text = (tmp_path / "cfg" / "opencode.json").read_text()
    assert "{env:VERDICT_WORKER_API_KEY}" in text
    # No sentinel key values should appear in the config
    assert "sk-" not in text
    assert "ant-" not in text


def test_write_attempt_config_includes_route_model(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_attempt_config(
        tmp_path / "cfg",
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((tmp_path / "cfg" / "opencode.json").read_text())
    models = cfg["providers"]["verdict"]["models"]
    assert ROUTE in models
    assert cfg["providers"]["verdict"]["settings"]["baseURL"] == "http://127.0.0.1:8000/v1"


def test_write_attempt_config_has_worker_marker_header(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_attempt_config(
        tmp_path / "cfg",
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((tmp_path / "cfg" / "opencode.json").read_text())
    headers = cfg["providers"]["verdict"].get("headers", {})
    assert headers.get("X-Verdict-Worker") == "opencode"


def test_write_attempt_config_has_external_directory_deny(tmp_path: Path) -> None:
    """V2 permission model: external_directory deny (not V1 Write/Edit rules)."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_attempt_config(
        tmp_path / "cfg",
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((tmp_path / "cfg" / "opencode.json").read_text())
    permissions = cfg.get("permissions", [])
    assert {"action": "external_directory", "resource": "*", "effect": "deny"} in permissions, (
        "permissions must contain the external_directory deny rule"
    )
    # V1 action names must not appear (they match nothing in V2)
    for rule in permissions:
        if isinstance(rule, dict):
            assert rule.get("action") not in ("Write", "Edit"), (
                "V1 action names Write/Edit must not appear in V2 permissions"
            )


def test_write_attempt_config_has_provider_deny_all_policy(tmp_path: Path) -> None:
    """Config must deny all provider.use and allow only verdict."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_attempt_config(
        tmp_path / "cfg",
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((tmp_path / "cfg" / "opencode.json").read_text())
    policies = cfg.get("policies", [])
    assert {"action": "provider.use", "resource": "*", "effect": "deny"} in policies, (
        "policies must contain a deny-all provider.use rule"
    )
    assert {"action": "provider.use", "resource": "verdict", "effect": "allow"} in policies, (
        "policies must allow verdict provider"
    )


def test_write_attempt_config_has_instructions_reference(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    _write_attempt_config(
        tmp_path / "cfg",
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((tmp_path / "cfg" / "opencode.json").read_text())
    instructions = cfg.get("instructions", [])
    expected_path = str(cwd / CONTEXT_FILE_NAME)
    assert any(expected_path in inst for inst in instructions), (
        f"instructions must reference {expected_path}"
    )


# ---------------------------------------------------------------------------
# _write_context_file
# ---------------------------------------------------------------------------


def test_write_context_file_creates_file_with_prompt(tmp_path: Path) -> None:
    _write_context_file(tmp_path, "Build the feature X.")
    content = (tmp_path / CONTEXT_FILE_NAME).read_text()
    assert "Build the feature X." in content


@pytest.mark.parametrize("target_exists", [True, False], ids=["existing-target", "dangling-target"])
def test_write_context_file_does_not_follow_symlink(tmp_path: Path, target_exists: bool) -> None:
    """A repo can track ``.verdict-context.md`` as a symlink to a path outside the worktree.

    Writing through it would truncate (or create) that outside file with the task
    prompt, using the orchestrator's permissions.  The write must replace the
    symlink itself and leave the outside target untouched.
    """
    worktree = tmp_path / "work"
    worktree.mkdir()
    outside = tmp_path / "outside-target.txt"
    if target_exists:
        outside.write_text("ORIGINAL OUTSIDE CONTENT")
    (worktree / CONTEXT_FILE_NAME).symlink_to(outside)

    _write_context_file(worktree, "Build the feature X.")

    if target_exists:
        assert outside.read_text() == "ORIGINAL OUTSIDE CONTENT", (
            "outside target was written through"
        )
    else:
        assert not outside.exists(), "dangling symlink target was created through the link"
    context = worktree / CONTEXT_FILE_NAME
    assert not context.is_symlink()
    assert "Build the feature X." in context.read_text()
    assert [p.name for p in worktree.iterdir()] == [CONTEXT_FILE_NAME], "temp file left behind"


def test_executor_does_not_write_through_context_symlink(tmp_path: Path) -> None:
    """Executor level: a tracked-symlink context file never modifies its outside target."""
    outside = tmp_path / "outside-target.txt"
    outside.write_text("ORIGINAL OUTSIDE CONTENT")
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / CONTEXT_FILE_NAME).symlink_to(outside)
    payload = ndjson_success("done")
    fake = make_fake_opencode(
        tmp_path, f"sys.stdout.write({payload!r})\nsys.stdout.write('\\n')\nsys.exit(0)\n"
    )
    executor = OpenCodeExecutor(opencode_bin=fake)
    terminal = run_executor(executor, "secret prompt text", cwd)
    assert terminal.ok is True
    assert outside.read_text() == "ORIGINAL OUTSIDE CONTENT"


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with no user/global config; return stdout."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo.parent),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _repo_tracking_context(tmp_path: Path, *, symlink: bool) -> tuple[Path, Path]:
    """A real git repo whose committed tree tracks ``.verdict-context.md``."""
    outside = tmp_path / "outside-target.txt"
    outside.write_text("ORIGINAL OUTSIDE CONTENT")
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    if symlink:
        (repo / CONTEXT_FILE_NAME).symlink_to(outside)
    else:
        (repo / CONTEXT_FILE_NAME).write_text("tracked content\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, outside


@pytest.mark.parametrize("symlink", [True, False], ids=["tracked-symlink", "tracked-file"])
def test_write_context_file_refuses_a_tracked_path(tmp_path: Path, symlink: bool) -> None:
    """A tracked ``.verdict-context.md`` must never be replaced.

    ``.git/info/exclude`` only hides untracked paths, so replacing a tracked entry
    would show up as a change that ``git add -A`` stages into the node deliverable
    (an ownership failure, or the task prompt committed if the path were owned).
    Refuse instead, and leave the index, working tree and outside target untouched.
    """
    repo, outside = _repo_tracking_context(tmp_path, symlink=symlink)
    before = _git(repo, "status", "--porcelain")

    with pytest.raises(OSError, match="tracked"):
        _write_context_file(repo, "task prompt that must not be written")

    assert _git(repo, "status", "--porcelain") == before == ""
    _git(repo, "add", "-A")
    assert _git(repo, "diff", "--cached", "--stat") == "", "staged diff changed"
    assert outside.read_text() == "ORIGINAL OUTSIDE CONTENT"
    assert [p.name for p in repo.iterdir() if p.name != ".git"] == [CONTEXT_FILE_NAME]


def test_executor_fails_closed_without_spawning_when_context_path_is_tracked(
    tmp_path: Path,
) -> None:
    """Executor level: a tracked context path ends the attempt before the worker starts."""
    repo, outside = _repo_tracking_context(tmp_path, symlink=True)
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    executor = OpenCodeExecutor(opencode_bin=fake)
    terminal = run_executor(executor, "secret prompt text", repo)
    assert terminal.ok is False
    assert terminal.error.startswith("context_write_failed")
    assert not ran.exists(), "worker was spawned despite the refused context write"
    assert _git(repo, "status", "--porcelain") == ""
    assert outside.read_text() == "ORIGINAL OUTSIDE CONTENT"


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired(cmd="git", timeout=30), FileNotFoundError("git")],
    ids=["git-timeout", "git-missing"],
)
def test_write_context_file_fails_closed_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """If tracking status cannot be determined, do not write: it may be tracked.

    A git timeout or missing executable must not read as "untracked" (that would
    let a tracked entry be replaced and then staged into the deliverable).
    """
    repo, _outside = _repo_tracking_context(tmp_path, symlink=False)
    real_run = subprocess.run

    def flaky_run(cmd: list[str], *args: object, **kwargs: object) -> object:
        if "ls-files" in cmd:
            raise failure
        return real_run(cmd, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("verdict.orchestration.executor_opencode.subprocess.run", flaky_run)
    with pytest.raises(OSError, match="cannot determine"):
        _write_context_file(repo, "task prompt that must not be written")
    assert (repo / CONTEXT_FILE_NAME).read_text() == "tracked content\n"
    assert sorted(p.name for p in repo.iterdir()) == sorted([".git", CONTEXT_FILE_NAME])


def test_write_context_file_fails_closed_when_git_index_is_unreadable(tmp_path: Path) -> None:
    """A failing ``git ls-files`` (nonzero exit) is indeterminate, not "untracked"."""
    repo, _outside = _repo_tracking_context(tmp_path, symlink=False)
    (repo / ".git" / "index").write_bytes(b"not a git index")
    with pytest.raises(OSError, match="cannot determine"):
        _write_context_file(repo, "task prompt that must not be written")
    assert (repo / CONTEXT_FILE_NAME).read_text() == "tracked content\n"
    assert sorted(p.name for p in repo.iterdir()) == sorted([".git", CONTEXT_FILE_NAME])


def test_executor_does_not_spawn_when_git_index_is_unreadable(tmp_path: Path) -> None:
    """Executor level: an indeterminate tracking check ends the attempt before spawn."""
    repo, _outside = _repo_tracking_context(tmp_path, symlink=False)
    (repo / ".git" / "index").write_bytes(b"not a git index")
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "prompt", repo)
    assert terminal.ok is False
    assert terminal.error.startswith("context_write_failed")
    assert not ran.exists(), "worker was spawned although tracking status was unknown"


def test_write_context_file_refuses_tracked_path_with_external_gitdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GIT_DIR``/``GIT_WORK_TREE`` make a directory a worktree with no ``.git`` entry.

    Looking for ``.git`` on disk would call this "not a repo" and overwrite a
    tracked context file that a later ``git add -A`` then stages.  Git itself must
    decide, under the same environment the runtime's git calls use.
    """
    work = tmp_path / "work"
    work.mkdir()
    gitdir = tmp_path / "external.git"
    (work / CONTEXT_FILE_NAME).write_text("tracked content\n")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_DIR": str(gitdir),
        "GIT_WORK_TREE": str(work),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "b"],
    ):
        subprocess.run(["git", *args], cwd=work, env=env, check=True, capture_output=True)
    assert not (work / ".git").exists(), "fixture must have no .git entry"
    monkeypatch.setenv("GIT_DIR", str(gitdir))
    monkeypatch.setenv("GIT_WORK_TREE", str(work))

    with pytest.raises(OSError, match="tracked"):
        _write_context_file(work, "task prompt that must not be written")
    assert (work / CONTEXT_FILE_NAME).read_text() == "tracked content\n"


def test_context_file_is_prepared_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The git tracking check can take seconds; it must not block the shared event loop."""
    import threading

    import verdict.orchestration.executor_opencode as module

    seen: list[bool] = []
    real = module._write_context_file

    def spy(cwd: Path, prompt: str) -> None:
        seen.append(threading.current_thread() is threading.main_thread())
        real(cwd, prompt)

    monkeypatch.setattr(module, "_write_context_file", spy)
    payload = ndjson_success("done")
    fake = make_fake_opencode(
        tmp_path, f"sys.stdout.write({payload!r})\nsys.stdout.write('\\n')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(
        OpenCodeExecutor(opencode_bin=fake), "prompt", cwd
    )  # loop runs in main thread
    assert terminal.ok is True
    assert seen == [False], "context preparation ran on the event-loop thread"


def test_cancelling_run_waits_for_the_context_preparation_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation must not return while a preparation thread can still write.

    The git probe runs in a worker thread that cannot be interrupted.  If ``run()``
    propagated ``CancelledError`` immediately, the caller could clean up or reuse the
    worktree while that thread later replaced ``.verdict-context.md`` (and a retry's
    newer context could be overwritten).  Block the probe, cancel ``run()``, and
    require that cancellation completes only after the write has finished.
    """
    import threading

    import verdict.orchestration.executor_opencode as module

    payload = ndjson_success("done")
    fake = make_fake_opencode(
        tmp_path, f"sys.stdout.write({payload!r})\nsys.stdout.write('\\n')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    started, release = threading.Event(), threading.Event()
    real_git = module._git

    def blocking_git(path: Path, *args: str) -> object:
        started.set()
        assert release.wait(timeout=30), "test never released the blocked git probe"
        return real_git(path, *args)

    monkeypatch.setattr(module, "_git", blocking_git)
    executor = OpenCodeExecutor(opencode_bin=fake)

    async def scenario() -> None:
        task = asyncio.ensure_future(
            executor.run("prompt", route_id=ROUTE, cwd=cwd, timeout_seconds=30.0)
        )
        for _ in range(500):  # up to 5 s for the thread to enter the probe
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "preparation thread never reached the git probe"
        task.cancel()
        await asyncio.sleep(0.3)
        assert not task.done(), "run() finished cancelling while the preparation thread still ran"
        assert not (cwd / CONTEXT_FILE_NAME).exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The write completed before cancellation did: nothing can write afterwards.
        assert (cwd / CONTEXT_FILE_NAME).exists()

    asyncio.run(scenario())


def test_output_over_the_limit_kills_the_worker_instead_of_buffering_it(tmp_path: Path) -> None:
    """A worker that prints without end must fail the attempt, not exhaust memory.

    The executor keeps worker output in memory for NDJSON parsing, and several
    attempts run concurrently, so total retained output is capped.  Without the cap
    this worker would run (and be buffered) until the attempt timeout.
    """
    pid_file = tmp_path / "writer_pid.txt"
    fake = make_fake_opencode(
        tmp_path,
        f"import pathlib\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"chunk = 'x' * 65536\n"
        f"while True:\n"
        f"    sys.stdout.write(chunk)\n"
        f"    sys.stdout.flush()\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(
        opencode_bin=fake, idle_output_seconds=120.0, max_output_bytes=256 * 1024
    )
    started = time.monotonic()
    terminal = run_executor(executor, "flood", cwd, timeout=60.0)
    assert time.monotonic() - started < 30.0, "worker was buffered until the timeout"
    assert terminal.ok is False
    assert terminal.error.startswith("output_limit"), terminal.error
    assert pid_file.exists()
    writer = int(pid_file.read_text())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not _pid_gone(writer):
        time.sleep(0.05)
    assert _pid_gone(writer), "worker process group was not killed on the output limit"


def test_context_preparation_time_counts_against_the_attempt_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow preparation (two git probes of up to 30 s each) must not extend the budget."""
    import verdict.orchestration.executor_opencode as module

    async def slow_prepare(cwd: Path, prompt: str) -> None:
        await asyncio.sleep(1.5)

    monkeypatch.setattr(module, "_prepare_context", slow_prepare)
    fake = make_fake_opencode(tmp_path, "time.sleep(60)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=120.0)
    started = time.monotonic()
    terminal = run_executor(executor, "slow prep", cwd, timeout=2.5)
    elapsed = time.monotonic() - started
    assert terminal.ok is False and terminal.error == "timeout", terminal
    # Budget 2.5 s in total: 1.5 s preparing leaves ~1.0 s for the worker.  Restarting the
    # full budget after preparation would take about 4 s.
    assert elapsed < 3.4, f"preparation time was not deducted from the budget ({elapsed:.1f}s)"


def test_attempt_times_out_without_spawning_when_preparation_used_the_whole_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.orchestration.executor_opencode as module

    async def slow_prepare(cwd: Path, prompt: str) -> None:
        await asyncio.sleep(0.6)

    monkeypatch.setattr(module, "_prepare_context", slow_prepare)
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "no budget", cwd, timeout=0.3)
    assert terminal.ok is False and terminal.error == "timeout", terminal
    assert not ran.exists(), "worker was spawned with no time left"


def test_write_context_file_still_replaces_an_untracked_symlink_in_a_git_repo(
    tmp_path: Path,
) -> None:
    """Untracked (so excluded) entries keep working: the symlink is replaced, not followed."""
    outside = tmp_path / "outside-target.txt"
    outside.write_text("ORIGINAL OUTSIDE CONTENT")
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / CONTEXT_FILE_NAME).symlink_to(outside)  # untracked
    _write_context_file(repo, "Build the feature X.")
    assert outside.read_text() == "ORIGINAL OUTSIDE CONTENT"
    assert not (repo / CONTEXT_FILE_NAME).is_symlink()


# ---------------------------------------------------------------------------
# _parse_ndjson_output
# ---------------------------------------------------------------------------


def test_parse_ndjson_output_extracts_usage() -> None:
    ndjson = ndjson_success("text output", "ses_test123")
    result = _parse_ndjson_output(ndjson)
    assert result["usage"] is not None
    assert result["usage"].input_tokens == 100
    assert result["usage"].output_tokens == 42


def test_parse_ndjson_output_no_usage_on_empty_input() -> None:
    result = _parse_ndjson_output("")
    assert result["usage"] is None
    assert result["session_id"] == ""


def test_parse_ndjson_output_survives_malformed_lines() -> None:
    garbage = "not-json\n{also bad}\n" + ndjson_success()
    result = _parse_ndjson_output(garbage)
    # Should succeed despite malformed leading lines
    assert result["usage"] is not None


def test_parse_ndjson_output_extracts_error_message() -> None:
    ndjson = json.dumps({"type": "error", "message": "rate limit exceeded", "sessionID": "ses_x"})
    result = _parse_ndjson_output(ndjson)
    assert result["error_message"] == "rate limit exceeded"


# ---------------------------------------------------------------------------
# _attempt_session_id
# ---------------------------------------------------------------------------


def test_attempt_session_id_starts_with_ses() -> None:
    sid = _attempt_session_id()
    assert sid.startswith("ses_")
    assert len(sid) > 8


def test_attempt_session_ids_are_unique() -> None:
    ids = {_attempt_session_id() for _ in range(10)}
    assert len(ids) == 10


# ---------------------------------------------------------------------------
# OpenCodeExecutor — integration with fake binary
# ---------------------------------------------------------------------------


def test_opencode_executor_success(tmp_path: Path) -> None:
    """Exit 0 + valid NDJSON → ok=True, usage extracted, harness=opencode."""
    payload = ndjson_success("feature implemented")
    fake = make_fake_opencode(
        tmp_path,
        f"import sys\nsys.stdout.write({payload!r})\nsys.stdout.write('\\n')\nsys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake)
    terminal = run_executor(executor, "ping", cwd)
    assert terminal.ok is True
    assert terminal.harness == "opencode"
    assert terminal.executor_kind == "live"
    assert terminal.usage is not None
    assert terminal.usage.input_tokens == 100


def test_opencode_executor_nonzero_exit_gives_failure(tmp_path: Path) -> None:
    """Non-zero exit → ok=False regardless of stdout content."""
    fake = make_fake_opencode(tmp_path, "import sys\nsys.stdout.write('{}\\n')\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake)
    terminal = run_executor(executor, "fail task", cwd)
    assert terminal.ok is False
    assert terminal.harness == "opencode"


def test_opencode_executor_uses_route_in_model_flag(tmp_path: Path) -> None:
    """The binary must be called with --model verdict/<route_id>."""
    capture = tmp_path / "argv.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, json\n"
        f"import pathlib\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake)
    run_executor(executor, "dummy", cwd)
    argv = json.loads(capture.read_text())
    model_idx = argv.index("--model")
    assert argv[model_idx + 1] == f"verdict/{ROUTE}"


def test_opencode_executor_sets_isolation_env_vars(tmp_path: Path) -> None:
    """Child receives OPENCODE_CONFIG_DIR, XDG_DATA_HOME, XDG_STATE_HOME."""
    capture = tmp_path / "env.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, json, os\n"
        f"import pathlib\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(
        opencode_bin=fake, env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    )
    run_executor(executor, "env test", cwd)
    child_env = json.loads(capture.read_text())
    assert "OPENCODE_CONFIG_DIR" in child_env
    assert "XDG_DATA_HOME" in child_env
    assert "XDG_STATE_HOME" in child_env
    assert child_env.get("OPENCODE_CONFIG_PROJECT_DISABLE") == "1"


def test_opencode_executor_credentials_not_in_child_env(tmp_path: Path) -> None:
    """OMNIROUTE_API_KEY and ANTHROPIC_* must not reach the child process."""
    capture = tmp_path / "env.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, json, os\n"
        f"import pathlib\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(
        opencode_bin=fake,
        env={
            "OMNIROUTE_API_KEY": "must-be-stripped",
            "OPENAI_API_KEY": "sk-stripped",
            "ANTHROPIC_API_KEY": "ant-stripped",
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    run_executor(executor, "cred test", cwd)
    child_env = json.loads(capture.read_text())
    assert "OMNIROUTE_API_KEY" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env


def test_opencode_executor_config_has_no_plaintext_key(tmp_path: Path) -> None:
    """The per-attempt config file must not contain a literal API key value."""
    capture = tmp_path / "config_path.txt"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, json, os, pathlib\n"
        f"config_dir = os.environ.get('OPENCODE_CONFIG_DIR', '')\n"
        f"cfg_file = pathlib.Path(config_dir) / 'opencode.json' if config_dir else None\n"
        f"if cfg_file and cfg_file.exists():\n"
        f"    pathlib.Path({str(capture)!r}).write_text(cfg_file.read_text())\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(
        opencode_bin=fake,
        api_key_env="VERDICT_WORKER_API_KEY",
        env={
            "VERDICT_WORKER_API_KEY": "real-secret-key-xyz",
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    run_executor(executor, "key test", cwd)
    assert capture.exists(), "capture file not written — fake binary failed to run"
    config_text = capture.read_text()
    assert "real-secret-key-xyz" not in config_text
    assert "{env:VERDICT_WORKER_API_KEY}" in config_text


def test_opencode_executor_writes_context_file(tmp_path: Path) -> None:
    """The context file must be written to the cwd before spawn."""
    capture = tmp_path / "context_exists.txt"
    prompt = "implement the foo feature"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, pathlib\n"
        f"ctx = pathlib.Path(sys.argv[-1]).parent / {CONTEXT_FILE_NAME!r}\n"
        # sys.argv[-1] is the prompt; cwd is the working directory of the script
        f"import os\n"
        f"ctx2 = pathlib.Path(os.getcwd()) / {CONTEXT_FILE_NAME!r}\n"
        f"pathlib.Path({str(capture)!r}).write_text(str(ctx2.exists()))\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake)
    run_executor(executor, prompt, cwd)
    # The context file must exist at the time the child runs
    assert capture.read_text().strip() == "True"
    # Context file contains the prompt
    ctx_path = cwd / CONTEXT_FILE_NAME
    assert ctx_path.exists()
    assert prompt in ctx_path.read_text()


def test_opencode_executor_idle_stall_returns_failure(tmp_path: Path) -> None:
    """Binary that produces no output for idle_output_seconds → stalled."""
    fake = make_fake_opencode(
        tmp_path,
        "import sys, time\n"
        "time.sleep(10)\n"  # much longer than idle timeout
        "sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=0.2)
    terminal = run_executor(executor, "stall test", cwd, timeout=5.0)
    assert terminal.ok is False
    assert "stalled" in (terminal.stop_reason or "") or "stalled" in (terminal.error or "")


def test_opencode_executor_timeout_returns_failure(tmp_path: Path) -> None:
    """Binary that runs past the hard timeout → ok=False."""
    fake = make_fake_opencode(
        tmp_path,
        # Keep writing output so it doesn't stall, but never exits
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('x')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    # Hard timeout of 0.3s, idle timeout generous so stall doesn't trigger first
    executor = OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=30.0)
    terminal = run_executor(executor, "timeout test", cwd, timeout=0.3)
    assert terminal.ok is False


def _pid_gone(pid: int) -> bool:
    """True when ``pid`` no longer exists or is only a zombie awaiting reaping."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # State is the first field after the parenthesised command name.
    return stat_text.rsplit(")", 1)[-1].split()[0] == "Z"


def test_kill_process_group_cleans_up_sigterm_ignoring_grandchild(tmp_path: Path) -> None:
    """_kill_process_group must kill the whole process group, not just the leader.

    Regression: the leader exits (and is reaped) but a grandchild that ignores
    SIGTERM survives in the recorded process group.  The old code looked the
    pgid up through the dead leader and so never sent SIGKILL to the group.

    Deterministic by construction: no idle or wall-clock timers race the fake
    process.  The test waits (generous deadline, host-load tolerant) for the
    grandchild to record its PID, reaps the leader, then calls
    ``_kill_process_group`` directly.
    """
    capture = tmp_path / "grandchild_pid.txt"
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import os, signal, sys, time\n"
        f"capture = {str(capture)!r}\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    tmp = capture + '.tmp'\n"
        "    with open(tmp, 'w') as fh:\n"
        "        fh.write(str(os.getpid()))\n"
        "    os.replace(tmp, capture)  # atomic: readers never see a partial PID\n"
        "    while True:\n"
        "        time.sleep(0.05)\n"
        "while not os.path.exists(capture):\n"
        "    time.sleep(0.02)\n"
        "sys.exit(0)\n"
    )

    async def scenario() -> int:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(leader),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # The leader exits only after the grandchild has recorded its PID.
        await asyncio.wait_for(proc.wait(), timeout=60.0)
        assert capture.exists(), "grandchild never recorded its PID"
        grandchild_pid = int(capture.read_text().strip())
        try:
            # Precondition: the grandchild outlives the reaped leader.
            assert not _pid_gone(grandchild_pid)
            await _kill_process_group(proc)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not _pid_gone(grandchild_pid):
                await asyncio.sleep(0.05)
            assert _pid_gone(grandchild_pid), (
                f"grandchild PID {grandchild_pid} still alive after _kill_process_group"
            )
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(grandchild_pid, signal.SIGKILL)  # never leak the test process
        return grandchild_pid

    asyncio.run(scenario())


def test_executor_kills_descendant_left_behind_by_normal_exit(tmp_path: Path) -> None:
    """A worker that exits 0 but leaves a detached-pipe descendant must not leave it running.

    Executor-level regression (not just the helper): the leader prints a valid
    result and exits 0 after its child has redirected stdin/stdout/stderr to
    /dev/null (so all pipe collection finishes normally) and ignores SIGTERM.
    ``run()`` must kill that descendant before returning, because owned-files
    validation starts as soon as it returns and a live descendant could still
    be editing the worktree.
    """
    capture = tmp_path / "descendant_pid.txt"
    payload = ndjson_success("done")
    fake = make_fake_opencode(
        tmp_path,
        f"import signal\n"
        f"capture = {str(capture)!r}\n"
        f"if os.fork() == 0:\n"
        f"    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    devnull = os.open(os.devnull, os.O_RDWR)\n"
        f"    for fd in (0, 1, 2):\n"
        f"        os.dup2(devnull, fd)\n"
        f"    with open(capture + '.tmp', 'w') as fh:\n"
        f"        fh.write(str(os.getpid()))\n"
        f"    os.replace(capture + '.tmp', capture)\n"
        f"    while True:\n"
        f"        time.sleep(0.05)\n"
        f"while not os.path.exists(capture):\n"
        f"    time.sleep(0.02)\n"
        f"sys.stdout.write({payload!r})\n"
        f"sys.stdout.write('\\n')\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    # Generous idle limit: host load must not trip the stall kill before the fork.
    executor = OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=120.0)
    descendant_pid = 0
    try:
        terminal = run_executor(executor, "descendant test", cwd, timeout=120.0)
        assert terminal.ok is True, f"worker should have succeeded: {terminal}"
        assert capture.exists(), "descendant never recorded its PID"
        descendant_pid = int(capture.read_text().strip())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _pid_gone(descendant_pid):
            time.sleep(0.05)
        assert _pid_gone(descendant_pid), (
            f"descendant PID {descendant_pid} still running after run() returned"
        )
    finally:
        if descendant_pid:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(descendant_pid, signal.SIGKILL)  # never leak the test process


_KEY = "sk-verdict-worker-SECRET-0123456789abcdef"
_OTHER_BEARER = "other-bearer-token-9999999999"


def _worker_env(tmp_path: Path) -> dict[str, str]:
    return {
        "VERDICT_WORKER_API_KEY": _KEY,
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }


def _leak_surfaces(terminal: object) -> str:
    """Everything the runtime could persist from a terminal, serialised.

    The runtime writes ``error``, ``output_tail`` and ``session_ref`` into the
    per-attempt evidence and events, and every other terminal field is plain
    data, so a JSON dump of the whole terminal is a superset of what can leak.
    """
    return json.dumps(dataclasses.asdict(terminal), default=str)  # type: ignore[call-overload]


@pytest.mark.parametrize("scenario", ["ndjson_error", "stderr_tail", "text_output"])
def test_executor_redacts_worker_key_everywhere(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    scenario: str,
) -> None:
    """The worker key and any ``Authorization: Bearer`` value never leave the executor.

    A fake worker echoes the key (and an unrelated bearer token) to stderr, into
    an NDJSON ``error`` event and into NDJSON ``text``.  Whichever of those the
    executor puts into the terminal, none of it may contain the secret values.
    """
    err = {
        "type": "error",
        "message": f"401 Unauthorized; sent Authorization: Bearer {_KEY} (key={_KEY})",
    }
    txt = {"type": "text", "text": f"echo {_KEY} / Authorization: Bearer {_OTHER_BEARER}"}
    stderr_text = (
        f"debug headers: Authorization: Bearer {_KEY}\n"
        f"retry with Authorization: Bearer {_OTHER_BEARER}\nkey={_KEY}\n"
    )
    lines = []
    if scenario == "ndjson_error":
        lines = [json.dumps(txt), json.dumps(err)]
    elif scenario == "text_output":
        lines = [json.dumps(txt), ndjson_success("done")]
    exit_code = 0 if scenario == "text_output" else 1
    fake = make_fake_opencode(
        tmp_path,
        f"sys.stderr.write({stderr_text!r})\n"
        f"for line in {lines!r}:\n"
        f"    sys.stdout.write(line + '\\n')\n"
        f"sys.exit({exit_code})\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, env=_worker_env(tmp_path))
    with caplog.at_level(logging.DEBUG):
        terminal = run_executor(executor, "redaction test", cwd)

    surfaces = _leak_surfaces(terminal)
    for secret in (_KEY, _OTHER_BEARER):
        assert secret not in surfaces, f"{secret!r} leaked into the terminal: {surfaces}"
        assert secret not in caplog.text, "secret leaked into captured logs"
    captured = capfd.readouterr()
    assert _KEY not in captured.out + captured.err
    # The failure is still diagnosable: a marker stands in for the secret.
    if exit_code:
        assert "[REDACTED]" in terminal.error  # type: ignore[attr-defined]
    else:
        assert "[REDACTED]" in terminal.output  # type: ignore[attr-defined]


def test_executor_redacts_key_before_truncating_stderr_tail(tmp_path: Path) -> None:
    """A key that straddles the tail-truncation window must not survive as a fragment.

    If the 400-character tail were cut first, only a suffix of the key would be
    left and an exact-match redaction would miss it.
    """
    stderr_text = _KEY + "x" * 380  # the 400-char tail would start inside the key
    fake = make_fake_opencode(tmp_path, f"sys.stderr.write({stderr_text!r})\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, env=_worker_env(tmp_path))
    terminal = run_executor(executor, "straddle test", cwd)
    assert terminal.ok is False
    for fragment in (_KEY, _KEY[10:], _KEY[-20:]):
        assert fragment not in terminal.error, f"key fragment {fragment!r} survived truncation"


def test_redact_helper_replaces_secrets_and_bearer_values() -> None:
    from verdict.orchestration.executor_opencode import _redact

    text = (
        f'x Authorization: Bearer abc.def-123 y "Authorization": "Bearer {_OTHER_BEARER}" k={_KEY}'
    )
    out = _redact(text, [_KEY, ""])
    assert _KEY not in out and "abc.def-123" not in out and _OTHER_BEARER not in out
    assert out.count("[REDACTED]") == 3
    assert _redact("plain text, no secrets", [_KEY]) == "plain text, no secrets"


def test_opencode_executor_spawn_failure_returns_terminal(tmp_path: Path) -> None:
    """A missing binary returns ok=False without raising."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin="/nonexistent/path/to/opencode-xyz")
    terminal = run_executor(executor, "spawn fail", cwd)
    assert terminal.ok is False
    assert terminal.harness == "opencode"
    assert terminal.error


def test_opencode_executor_temp_dirs_cleaned_up(tmp_path: Path) -> None:
    """Temp config dir is removed after the executor finishes."""
    capture = tmp_path / "dirs.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, json, os\n"
        f"import pathlib\n"
        f"pathlib.Path({str(capture)!r}).write_text(\n"
        f"    json.dumps({{'config': os.environ.get('OPENCODE_CONFIG_DIR', ''),\n"
        f"               'data': os.environ.get('XDG_DATA_HOME', ''),\n"
        f"               'state': os.environ.get('XDG_STATE_HOME', ''),\n"
        f"              }})\n"
        f")\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(
        opencode_bin=fake, env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    )
    run_executor(executor, "cleanup test", cwd)
    assert capture.exists(), "capture file not written — fake binary failed to run"
    dirs = json.loads(capture.read_text())
    for key, path in dirs.items():
        parent = Path(path).parent if path else None
        if parent and "verdict-oc-" in str(parent):
            assert not Path(path).parent.exists(), f"Temp dir {path!r} ({key}) was not cleaned up"


# ---------------------------------------------------------------------------
# PINNED_VERSION sanity
# ---------------------------------------------------------------------------


def test_pinned_version_format() -> None:
    parts = PINNED_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
