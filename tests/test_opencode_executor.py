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
from datetime import datetime, timezone
from pathlib import Path

import pytest

import verdict.orchestration.executor_opencode as executor_module
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

# The real pre-dispatch functions, captured before the autouse fixture replaces them.
_REAL_WORKER_CREDENTIAL = executor_module._worker_credential
_REAL_CHECK_BINARY = executor_module._check_binary
_REAL_CHECK_SERVE = executor_module._check_serve


@pytest.fixture(autouse=True)
def _serve_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default for every test here: a serve credential resolves, the binary runs, serve accepts.

    The fake workers are scripts that record what they are given, so the real binary check
    (which runs ``--version``) is replaced too.  The tests of the pre-dispatch checks put the
    real functions back.  Nothing here ever reads a real credential store or contacts a real
    serve.
    """
    monkeypatch.setattr(
        executor_module, "_worker_credential", lambda *a, **k: "fixture-serve-token-0000"
    )
    monkeypatch.setattr(executor_module, "_check_binary", lambda *a, **k: None)
    monkeypatch.setattr(executor_module, "_check_serve", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _procfs_available(root: Path = Path("/proc")) -> bool:
    """True when process state can be read from procfs (``_pid_gone`` depends on it)."""
    return (root / "self" / "stat").exists()


requires_procfs = pytest.mark.skipif(
    not _procfs_available(), reason="process liveness is read from procfs"
)


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

    def spy(cwd: Path, prompt: str, *args: object) -> None:
        seen.append(threading.current_thread() is threading.main_thread())
        real(cwd, prompt, *args)  # type: ignore[arg-type]

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

    def blocking_git(path: Path, *args: str, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=30), "test never released the blocked git probe"
        return real_git(path, *args, **kwargs)  # type: ignore[arg-type]

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


@requires_procfs
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
    try:
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
    finally:
        # Whatever failed above, never leave an endless writer running.
        recorded = pid_file.read_text().strip() if pid_file.exists() else ""
        if recorded.isdigit():
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(int(recorded), signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(int(recorded), signal.SIGKILL)


def test_collect_does_not_spin_when_one_pipe_closes_and_the_worker_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished pump must not make the wait return immediately on every iteration.

    The worker closes stdout and carries on silently.  The collector should then sleep until
    the idle timer fires; a wait set that still contains the finished task would return at
    once, again and again, burning CPU on the event loop shared by every worker.
    """
    fake = make_fake_opencode(tmp_path, "os.close(1)\ntime.sleep(60)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    calls = 0
    real_wait = asyncio.wait

    async def counting_wait(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return await real_wait(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "wait", counting_wait)
    executor = OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=1.5)
    terminal = run_executor(executor, "closes stdout", cwd, timeout=30.0)
    assert terminal.ok is False and terminal.error.startswith("stalled"), terminal
    assert calls < 50, f"collector woke {calls} times while idle (busy loop)"


def test_slow_git_probe_is_bounded_by_the_attempt_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The git tracking probes must stop at the attempt deadline, not at their own 30 s.

    Deducting elapsed time only after preparation would still let a short attempt take up
    to a minute before returning (and cancellation waits for the preparation thread).  The
    remaining budget is passed into each probe.  A fake ``git`` that hangs stands in for a
    stuck repository.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    slow_git = fake_bin / "git"
    slow_git.write_text(
        "#!/bin/sh\nexec sleep 60\n"
    )  # exec: the killed PID is the sleeper, no orphan
    slow_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    started = time.monotonic()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "hung git", cwd, timeout=1.5)
    elapsed = time.monotonic() - started
    assert terminal.ok is False and terminal.error == "timeout", terminal
    assert elapsed < 10.0, (
        f"hung git probe was not bounded by the attempt deadline ({elapsed:.1f}s)"
    )
    assert not ran.exists(), "worker was spawned after the deadline"


def test_context_preparation_time_counts_against_the_attempt_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow preparation (two git probes of up to 30 s each) must not extend the budget."""
    import verdict.orchestration.executor_opencode as module

    async def slow_prepare(cwd: Path, prompt: str, deadline: float | None = None) -> None:
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

    async def slow_prepare(cwd: Path, prompt: str, deadline: float | None = None) -> None:
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
    """True when ``pid`` no longer exists, or is only a zombie or dead entry awaiting reaping.

    Reaping can race the probe: ``kill`` may succeed and the ``/proc`` read then fail (ENOENT,
    or ESRCH from an already-open entry).  Such a failure is retried; the next ``kill`` then
    reports the process as gone.
    """
    for _ in range(5):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            continue  # raced with reaping: probe again
        # State is the first field after the parenthesised command name.
        return stat_text.rsplit(")", 1)[-1].split()[0] in {"Z", "X"}
    return False


@requires_procfs
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

    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(leader),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            # The leader exits only after the grandchild has recorded its PID.
            await asyncio.wait_for(proc.wait(), timeout=60.0)
            assert capture.exists(), "grandchild never recorded its PID"
            grandchild_pid = int(capture.read_text().strip())
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
            # Unconditional, and covering a timeout or a failed assertion above: kill the whole
            # group (the grandchild is in it), then reap the leader.
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

    asyncio.run(scenario())


@requires_procfs
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
        # Recover the PID from the capture file whatever failed above (a failed or raised
        # run() must not leave the SIGTERM-ignoring descendant running indefinitely).
        recorded = capture.read_text().strip() if capture.exists() else ""
        if recorded.isdigit():
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(int(recorded), signal.SIGKILL)  # never leak the test process


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
    # The assertion must never be skipped: every isolation path must have been set, and all
    # three must live under one per-attempt root (otherwise a persistent directory was used).
    assert set(dirs) == {"config", "data", "state"}
    assert all(dirs.values()), f"an isolation env var was empty: {dirs}"
    roots = {Path(path).parent for path in dirs.values()}
    assert len(roots) == 1, f"isolation dirs are not under one per-attempt root: {dirs}"
    (root,) = roots
    assert root.name.startswith("verdict-oc-"), f"not a per-attempt temp root: {root}"
    assert not root.exists(), f"per-attempt temp root {str(root)!r} was not cleaned up"


def test_two_concurrent_attempts_never_share_config_data_or_state(tmp_path: Path) -> None:
    """Task 2.3 isolation: workers that run at the same time each get their own config, data
    (session DB) and state directories, and each finds its own written config while both run; every
    directory is removed afterwards.

    The fake worker records its own directories and then waits until the other worker has
    recorded theirs, so the two attempts genuinely overlap.
    """
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    fake = make_fake_opencode(
        tmp_path,
        f"""
        import pathlib
        record_dir = pathlib.Path({str(record_dir)!r})
        session = sys.argv[sys.argv.index('--session') + 1]
        dirs = {{k: os.environ.get(v, '') for k, v in
                 (('config', 'OPENCODE_CONFIG_DIR'), ('data', 'XDG_DATA_HOME'),
                  ('state', 'XDG_STATE_HOME'))}}
        dirs['config_present'] = os.path.isfile(os.path.join(dirs['config'], 'opencode.json'))
        (record_dir / (session + '.json')).write_text(json.dumps(dirs))
        deadline = time.monotonic() + 8
        while len(list(record_dir.glob('*.json'))) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        sys.exit(0 if len(list(record_dir.glob('*.json'))) == 2 else 3)
        """,
    )
    cwds = []
    for name in ("one", "two"):
        cwd = tmp_path / name
        cwd.mkdir()
        cwds.append(cwd)
    executor = OpenCodeExecutor(
        opencode_bin=fake, env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    )

    async def both() -> list[object]:
        return list(
            await asyncio.gather(
                *(
                    executor.run("task", route_id=ROUTE, cwd=cwd, timeout_seconds=20.0)
                    for cwd in cwds
                )
            )
        )

    terminals = asyncio.run(both())
    assert [t.ok for t in terminals] == [True, True], [t.error for t in terminals]
    records = [json.loads(path.read_text()) for path in sorted(record_dir.glob("*.json"))]
    assert len(records) == 2, "the two workers did not overlap"
    paths = [r[k] for r in records for k in ("config", "data", "state")]
    assert all(paths), f"an isolation variable was empty: {records}"
    assert len(set(paths)) == 6, f"two concurrent attempts shared a directory: {records}"
    assert len({Path(p).parent for p in paths}) == 2, "each attempt needs its own root"
    assert [r["config_present"] for r in records] == [True, True], "a worker ran without its config"
    assert not any(Path(p).exists() for p in paths), "an attempt directory outlived its attempt"


# ---------------------------------------------------------------------------
# A full or unwritable temp filesystem: run() never raises
# ---------------------------------------------------------------------------


def _spawn_recorder(tmp_path: Path) -> tuple[str, Path]:
    """A fake binary that records that it ran (it must not, in the tests that use this)."""
    marker = tmp_path / "worker-ran"
    return (
        make_fake_opencode(
            tmp_path,
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\nsys.exit(0)\n",
        ),
        marker,
    )


def test_a_full_temp_filesystem_is_a_failed_terminal_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run()`` promises to return a terminal.  Creating the per-attempt directory used to
    sit outside the error handling, so ENOSPC (or an unwritable TMPDIR) escaped as OSError."""
    import errno
    import tempfile

    real = tempfile.TemporaryDirectory

    def full_disk(*args: object, **kwargs: object) -> object:
        if kwargs.get("prefix") == "verdict-oc-":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", full_disk)
    fake, marker = _spawn_recorder(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.harness == "opencode"
    assert terminal.error.startswith("config_write_failed:"), terminal.error
    assert "No space left" in terminal.error
    assert not marker.exists(), "the worker was launched without its per-attempt directory"


def test_a_failed_config_write_is_a_terminal_and_the_attempt_directory_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    created: list[str] = []

    def full_disk(config_dir: Path, **_kwargs: object) -> None:
        created.append(str(config_dir.parent))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(executor_module, "_write_attempt_config", full_disk)
    fake, marker = _spawn_recorder(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False and terminal.error.startswith("config_write_failed:")
    assert len(created) == 1 and Path(created[0]).name.startswith("verdict-oc-")
    assert not Path(created[0]).exists(), "the per-attempt directory outlived the failed attempt"
    assert not marker.exists()


@pytest.mark.parametrize("worker_ok", [True, False], ids=["worker-succeeds", "worker-fails"])
def test_a_failing_directory_cleanup_does_not_replace_the_result_and_still_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_ok: bool
) -> None:
    """Cleanup runs in a ``finally``; an OSError from it (a busy or unreadable entry on a
    full disk) must not turn a finished attempt into an exception, and the directory is
    still removed on a best-effort basis."""
    import errno
    import tempfile

    real = tempfile.TemporaryDirectory
    roots: list[str] = []

    class FailingCleanup(real):  # type: ignore[type-arg, misc]
        def cleanup(self) -> None:
            roots.append(self.name)
            raise OSError(errno.EBUSY, "Device or resource busy")

    def build(*args: object, **kwargs: object) -> object:
        if kwargs.get("prefix") == "verdict-oc-":
            return FailingCleanup(*args, **kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", build)
    fake = make_fake_opencode(tmp_path, f"sys.exit({0 if worker_ok else 1})\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is worker_ok
    assert len(roots) == 1
    assert not Path(roots[0]).exists(), "the attempt directory was left behind"


# ---------------------------------------------------------------------------
# Serve credential and pre-dispatch checks (review item 1)
# ---------------------------------------------------------------------------

_SERVE_TOKEN = "serve-token-UNIQUE-4f9a1c"


def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real credential store, serve.env or exported token can be read by the test."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / "verdict"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in ("LLMGATE_AUTH_TOKEN", "OMNIROUTE_API_KEY", "VERDICT_OMNIROUTE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class _FakeServe:
    """A local stand-in for ``verdict serve``'s readiness endpoint (no model, no upstream)."""

    def __init__(self, accepts: str, status_if_rejected: int = 401) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        accepted = f"Bearer {accepts}"
        outer = self
        self.requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append(self.path)
                ok = self.path == "/v1/guidance/status" and (
                    self.headers.get("Authorization") == accepted
                )
                self.send_response(200 if ok else status_if_rejected)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_serve_credential_reaches_only_the_child_env_under_the_configured_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved by Verdict's own code (here via serve's documented LLMGATE_AUTH_TOKEN) and
    handed to the worker only as VERDICT_WORKER_API_KEY: not in argv, the config file, the
    context file, or any other variable."""
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", _SERVE_TOKEN)
    monkeypatch.setattr(executor_module, "_worker_credential", _REAL_WORKER_CREDENTIAL)
    capture = tmp_path / "capture.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import pathlib\n"
        f"cfg = pathlib.Path(os.environ['OPENCODE_CONFIG_DIR'])\n"
        f"files = {{p.name: p.read_text() for p in cfg.iterdir() if p.is_file()}}\n"
        f"ctx = pathlib.Path(os.getcwd()) / '.verdict-context.md'\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{\n"
        f"    'env': dict(os.environ), 'argv': sys.argv, 'config': files,\n"
        f"    'context': ctx.read_text() if ctx.exists() else '',\n"
        f"}}))\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "do the task", cwd)
    assert terminal.ok is True, terminal
    data = json.loads(capture.read_text())
    holders = [name for name, value in data["env"].items() if _SERVE_TOKEN in value]
    assert holders == ["VERDICT_WORKER_API_KEY"], holders
    for surface in ("argv", "config", "context"):
        assert _SERVE_TOKEN not in json.dumps(data[surface]), f"credential leaked into {surface}"
    assert "{env:VERDICT_WORKER_API_KEY}" in json.dumps(data["config"])


def test_the_omniroute_gateway_key_is_never_the_worker_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OMNIROUTE_API_KEY", "omniroute-gateway-key-NOT-FOR-WORKERS")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "omniroute-gateway-key-NOT-FOR-WORKERS")
    assert _REAL_WORKER_CREDENTIAL("cc/claude-sonnet-5", "ses_x") is None


def test_missing_serve_credential_fails_closed_before_anything_is_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(executor_module, "_worker_credential", _REAL_WORKER_CREDENTIAL)
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: no verdict serve credential")
    assert not ran.exists(), "the worker was launched without a credential"
    assert list(cwd.iterdir()) == [], "the worktree was written to before the pre-dispatch check"


def test_pre_dispatch_failure_is_classified_as_a_block_not_a_retry(tmp_path: Path) -> None:
    """One message, no burnt attempts: recovery maps ``harness_configuration:`` to BLOCK."""
    from verdict.orchestration.recovery import FailureIntelligence

    terminal = run_executor(
        OpenCodeExecutor(
            opencode_bin=make_fake_opencode(tmp_path, "sys.exit(0)\n"),
            credential_provider=lambda route, attempt: None,
        ),
        "task",
        tmp_path,
    )
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action) == ("harness_configuration", "BLOCK")


@pytest.mark.parametrize("flavour", ["missing", "not-executable", "exits-nonzero"])
def test_unrunnable_binary_fails_closed_before_launch(tmp_path: Path, flavour: str) -> None:
    if flavour == "missing":
        binary = str(tmp_path / "no-such-opencode")
    elif flavour == "not-executable":
        script = tmp_path / "opencode-plain-file"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)
        binary = str(script)
    else:
        script = tmp_path / "opencode-broken"
        script.write_text("#!/bin/sh\nexit 3\n")
        script.chmod(0o755)
        binary = str(script)
    assert _REAL_CHECK_BINARY(binary) is not None
    executor_module_check = executor_module._check_binary
    executor_module._check_binary = _REAL_CHECK_BINARY  # restored by the autouse fixture
    try:
        cwd = tmp_path / "work"
        cwd.mkdir()
        terminal = run_executor(OpenCodeExecutor(opencode_bin=binary), "task", cwd)
    finally:
        executor_module._check_binary = executor_module_check
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: opencode binary"), terminal.error
    assert list(cwd.iterdir()) == [], "the worktree was written to before the binary check"


def test_binary_probe_runs_with_a_minimal_environment_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version probe needs no credential: the serve token and provider keys exported in the
    orchestrator's environment must not reach it (the worker's allowlist drops them too)."""
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", _SERVE_TOKEN)
    monkeypatch.setenv("OMNIROUTE_API_KEY", "omniroute-key-for-the-probe-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-key-for-the-probe-test")
    dump = tmp_path / "probe-env.json"
    script = tmp_path / "opencode-env-dump"
    script.write_text(
        "#!/usr/bin/env python3\n"
        f"import json, os, pathlib\npathlib.Path({str(dump)!r}).write_text(json.dumps(dict(os.environ)))\n"
        "print('2.3.2')\n"
    )
    script.chmod(0o755)
    assert _REAL_CHECK_BINARY(str(script)) is None
    seen = json.loads(dump.read_text())
    leaked = [
        k
        for k, v in seen.items()
        if any(x in v for x in (_SERVE_TOKEN, "omniroute-key", "provider-key"))
    ]
    assert leaked == [], f"credentials reached the version probe: {leaked}"
    assert "PATH" in seen  # still runnable


@requires_procfs
def test_binary_probe_kills_a_helper_it_leaves_holding_the_pipes(tmp_path: Path) -> None:
    """``subprocess.run`` kills only the direct child; a forked helper that keeps stdout open
    must not hold the probe (or survive it)."""
    pid_file = tmp_path / "helper.pid"
    script = tmp_path / "opencode-forks-a-helper"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > '{pid_file}'\necho 2.3.2\n")
    script.chmod(0o755)
    started = time.monotonic()
    try:
        assert _REAL_CHECK_BINARY(str(script), timeout=2.0) is None
        assert time.monotonic() - started < 10.0, "the probe waited for the helper's pipes"
        helper = int(pid_file.read_text())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _pid_gone(helper):
            time.sleep(0.05)
        assert _pid_gone(helper), "the probe left its helper running"
    finally:
        recorded = pid_file.read_text().strip() if pid_file.exists() else ""
        if recorded.isdigit():
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(int(recorded), signal.SIGKILL)


def test_pre_dispatch_checks_are_bounded_by_the_attempt_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow ``--version`` must not hold an attempt past its own budget; running out of
    budget is a ``timeout``, not a configuration block."""
    script = tmp_path / "opencode-slow-version"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(executor_module, "_check_binary", _REAL_CHECK_BINARY)
    cwd = tmp_path / "work"
    cwd.mkdir()
    started = time.monotonic()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=str(script)), "task", cwd, timeout=1.0)
    assert time.monotonic() - started < 8.0, "the binary check ignored the attempt deadline"
    assert terminal.ok is False and terminal.error == "timeout", terminal
    assert list(cwd.iterdir()) == []


def test_a_failing_credential_lookup_is_a_configuration_failure_not_an_exception(
    tmp_path: Path,
) -> None:
    def exploding(route: str, attempt: str) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "malformed credentials file")

    fake = make_fake_opencode(tmp_path, "sys.exit(0)\n")
    terminal = run_executor(
        OpenCodeExecutor(opencode_bin=fake, credential_provider=exploding), "task", tmp_path
    )
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: serve credential lookup failed")
    assert "UnicodeDecodeError" in terminal.error


def test_runnable_binary_passes_the_real_check(tmp_path: Path) -> None:
    script = tmp_path / "opencode-ok"
    script.write_text("#!/bin/sh\necho 2.3.2\n")
    script.chmod(0o755)
    assert _REAL_CHECK_BINARY(str(script)) is None


def test_serve_readiness_check_accepts_a_reachable_serve_that_takes_the_credential() -> None:
    serve = _FakeServe(accepts=_SERVE_TOKEN)
    try:
        assert _REAL_CHECK_SERVE(serve.origin, _SERVE_TOKEN, 5.0) is None
        assert serve.requests == ["/v1/guidance/status"]
    finally:
        serve.close()


@pytest.mark.parametrize("status", [401, 403])
def test_serve_readiness_check_reports_a_rejected_credential_without_echoing_it(
    status: int,
) -> None:
    serve = _FakeServe(accepts="some-other-token", status_if_rejected=status)
    try:
        reason = _REAL_CHECK_SERVE(serve.origin, _SERVE_TOKEN, 5.0)
    finally:
        serve.close()
    assert reason is not None and f"HTTP {status}" in reason and "rejected" in reason
    assert _SERVE_TOKEN not in reason


def test_serve_readiness_check_reports_an_unreachable_serve() -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens here
    reason = _REAL_CHECK_SERVE(f"http://127.0.0.1:{port}", _SERVE_TOKEN, 2.0)
    assert reason is not None and "unreachable" in reason and _SERVE_TOKEN not in reason


def test_executor_stops_before_launch_when_serve_rejects_the_credential(tmp_path: Path) -> None:
    serve = _FakeServe(accepts="some-other-token")
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    saved = executor_module._check_serve
    executor_module._check_serve = _REAL_CHECK_SERVE  # restored by the autouse fixture
    try:
        executor = OpenCodeExecutor(
            opencode_bin=fake,
            serve_base_url=serve.origin,
            credential_provider=lambda route, attempt: _SERVE_TOKEN,
        )
        terminal = run_executor(executor, "task", cwd)
    finally:
        executor_module._check_serve = saved
        serve.close()
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: verdict serve at ")
    assert "rejected the worker credential" in terminal.error
    assert _SERVE_TOKEN not in terminal.error
    assert not ran.exists()
    assert list(cwd.iterdir()) == []


def test_serve_credential_value_never_reaches_terminal_events_or_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The worker echoes the credential it was given (from its env) to stderr and into NDJSON."""
    monkeypatch.setattr(executor_module, "_worker_credential", lambda *a, **k: _SERVE_TOKEN)
    fake = make_fake_opencode(
        tmp_path,
        "tok = os.environ['VERDICT_WORKER_API_KEY']\n"
        "sys.stderr.write('debug Authorization: Bearer ' + tok + ' key=' + tok + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'error', 'error': {'message': 'bad ' + tok}}) + '\\n')\n"
        "sys.exit(1)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    with caplog.at_level(logging.DEBUG):
        terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert _SERVE_TOKEN not in json.dumps(dataclasses.asdict(terminal), default=str)
    assert _SERVE_TOKEN not in caplog.text
    captured = capfd.readouterr()
    assert _SERVE_TOKEN not in captured.out + captured.err
    assert "[REDACTED]" in terminal.error


# ---------------------------------------------------------------------------
# Serve address (review item 11)
# ---------------------------------------------------------------------------


def test_serve_address_comes_from_configuration_and_defaults_to_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERDICT_SERVE_URL", raising=False)
    assert OpenCodeExecutor()._serve_base_url == "http://127.0.0.1:8000"
    monkeypatch.setenv("VERDICT_SERVE_URL", "http://127.0.0.1:9123/v1")
    assert OpenCodeExecutor()._serve_base_url == "http://127.0.0.1:9123"
    # An explicit address (the --serve-url flag) wins over the variable.
    assert OpenCodeExecutor(serve_base_url="http://localhost:9555")._serve_base_url == (
        "http://localhost:9555"
    )


def test_the_configured_serve_address_is_what_the_worker_calls(tmp_path: Path) -> None:
    capture = tmp_path / "config.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import pathlib\n"
        f"cfg = pathlib.Path(os.environ['OPENCODE_CONFIG_DIR']) / 'opencode.json'\n"
        f"pathlib.Path({str(capture)!r}).write_text(cfg.read_text())\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    run_executor(
        OpenCodeExecutor(opencode_bin=fake, serve_base_url="http://127.0.0.1:9123"), "t", cwd
    )
    config = json.loads(capture.read_text())
    assert config["providers"]["verdict"]["settings"]["baseURL"] == "http://127.0.0.1:9123/v1"


# ---------------------------------------------------------------------------
# Upstream HTTP status and cost (review items 4 and 12)
# ---------------------------------------------------------------------------


def _ndjson_error(status: int, message: str, retry_after_ms: int | None = None) -> str:
    detail: dict[str, object] = {"type": "provider.error", "message": message, "status": status}
    if retry_after_ms is not None:
        detail["retryAfterMs"] = retry_after_ms
    return json.dumps({"type": "error", "error": detail})


@pytest.mark.parametrize(("status", "retry_ms"), [(429, 7000), (503, None), (500, None)])
def test_structured_error_keeps_its_http_status_and_retry_delay(
    tmp_path: Path, status: int, retry_ms: int | None
) -> None:
    """OpenCode reports an upstream failure as {"error": {"status", "message", "retryAfterMs"}}.

    The status must reach recovery, or a 429/503 is an unclassified failure instead of a
    reroutable one.
    """
    line = _ndjson_error(status, "upstream said no", retry_ms)
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.status_code == status
    assert terminal.retry_after_seconds == (retry_ms / 1000 if retry_ms else None)
    assert terminal.error == "upstream said no"


def test_status_in_plain_error_text_is_still_recognised(tmp_path: Path) -> None:
    line = json.dumps({"type": "error", "message": "HTTP 503 Service Unavailable"})
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.status_code == 503


# A diagnostic long enough that the 400-character tail kept for the terminal cannot contain the
# status that opens the message.
_LONG_DIAGNOSTIC = " provider diagnostic: the upstream closed the request before it finished." * 30


@pytest.mark.parametrize(
    ("event", "status"),
    [
        ({"type": "error", "message": f"HTTP 429: rate limited.{_LONG_DIAGNOSTIC}"}, 429),
        ({"type": "error", "error": f"HTTP 503 Service Unavailable.{_LONG_DIAGNOSTIC}"}, 503),
        ({"type": "error", "error": {"message": f"HTTP 502 Bad Gateway.{_LONG_DIAGNOSTIC}"}}, 502),
    ],
    ids=["message", "error-string", "error-object-without-status"],
)
def test_a_status_in_a_long_unstructured_error_is_not_lost_to_truncation(
    tmp_path: Path, event: dict[str, object], status: int
) -> None:
    """The error text kept on the terminal is its last 400 characters, but the HTTP status is
    read from the whole text first, so a long provider diagnostic cannot hide a 429 or 503
    from recovery."""
    line = json.dumps(event)
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.status_code == status
    assert len(terminal.error) <= 400, "the recorded error text must still be truncated"
    assert f"HTTP {status}" not in terminal.error, "the test needs the status cut from the tail"


def test_a_status_in_a_long_stderr_is_not_lost_to_truncation(tmp_path: Path) -> None:
    """The same holds for a failure that printed no NDJSON error: the status is read from the
    whole sanitized stderr before its tail is kept."""
    text = f"fatal: HTTP 504 Gateway Timeout.{_LONG_DIAGNOSTIC}"
    fake = make_fake_opencode(tmp_path, f"sys.stderr.write({text!r})\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.status_code == 504
    assert len(terminal.error) <= 400
    assert "HTTP 504" not in terminal.error


def test_a_structured_status_still_wins_over_a_number_in_the_text() -> None:
    line = json.dumps(
        {"type": "error", "error": {"status": 429, "message": f"retry in 503 ms{_LONG_DIAGNOSTIC}"}}
    )
    assert _parse_ndjson_output(line)["error_status"] == 429


def test_a_status_in_the_tail_still_beats_an_earlier_stray_number_in_stderr(
    tmp_path: Path,
) -> None:
    """Reading the whole text must not change an answer the tail already gave.

    The last 400 characters were always searched, and a status there wins; only when the tail
    has none is the rest of the text read.  A stray ``500`` (a duration) at the start of a long
    stderr must not beat the real ``HTTP 429`` at its end.
    """
    text = f"retrying in 500 ms.{_LONG_DIAGNOSTIC} fatal: HTTP 429 Too Many Requests"
    fake = make_fake_opencode(tmp_path, f"sys.stderr.write({text!r})\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.status_code == 429
    assert "HTTP 429" in terminal.error


def test_a_status_in_the_tail_still_beats_an_earlier_stray_number_in_an_ndjson_message(
    tmp_path: Path,
) -> None:
    line = json.dumps(
        {
            "type": "error",
            "message": f"retrying in 500 ms.{_LONG_DIAGNOSTIC} fatal: HTTP 429 Too Many Requests",
        }
    )
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.status_code == 429


def test_a_status_from_an_earlier_error_event_does_not_stick_to_a_later_message() -> None:
    """The status belongs to the event whose message is kept: a later error without a status
    of its own must not inherit the first one's."""
    first = json.dumps({"type": "error", "message": "HTTP 429 rate limited"})
    second = json.dumps({"type": "error", "message": "the provider crashed"})
    parsed = _parse_ndjson_output(first + "\n" + second)
    assert parsed["error_message"] == "the provider crashed"
    assert parsed["error_status"] is None


def test_a_secret_in_the_head_of_a_long_error_never_reaches_the_terminal(tmp_path: Path) -> None:
    """Parsing the status from the full text must not let the full text out: the secret sits
    in the part that is cut away and in the part that is kept, and neither is recorded."""
    secret = "sk-test-LONGERROR-5e1d9c"
    line = json.dumps(
        {"type": "error", "message": f"HTTP 429 key={secret}.{_LONG_DIAGNOSTIC} key={secret}"}
    )
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, credential_provider=lambda *_a: secret)
    terminal = run_executor(executor, "task", cwd)
    assert terminal.status_code == 429
    assert secret not in json.dumps(dataclasses.asdict(terminal), default=str)


def _step_finish(cost: object, tokens_in: int = 100, tokens_out: int = 42) -> str:
    event: dict[str, object] = {
        "type": "step_finish",
        "tokens": {"input": tokens_in, "output": tokens_out},
    }
    if cost is not None:
        event["cost"] = cost
    return json.dumps(event)


@pytest.mark.parametrize("reported", [0, 0.0, {"total": 0}])
def test_a_reported_zero_cost_is_never_recorded_as_zero(reported: object) -> None:
    parsed = _parse_ndjson_output(_step_finish(reported))
    usage = parsed["usage"]
    assert usage.cost_usd is None
    assert usage.cost_note and "0" in usage.cost_note
    assert (usage.input_tokens, usage.output_tokens) == (100, 42)  # tokens are still recorded


def test_a_missing_cost_is_unknown_with_a_reason() -> None:
    usage = _parse_ndjson_output(_step_finish(None))["usage"]
    assert usage.cost_usd is None and usage.cost_note
    assert usage.input_tokens == 100


def test_a_positive_cost_is_recorded_and_summed() -> None:
    stream = "\n".join([_step_finish(0.0123), _step_finish({"total": 0.0077})])
    usage = _parse_ndjson_output(stream)["usage"]
    assert usage.cost_usd == pytest.approx(0.02)
    assert usage.cost_note is None
    assert usage.input_tokens == 200


def test_one_unpriced_step_makes_the_whole_attempt_cost_unknown() -> None:
    stream = "\n".join([_step_finish(0.5), _step_finish(0)])
    usage = _parse_ndjson_output(stream)["usage"]
    assert usage.cost_usd is None
    assert usage.cost_note


# ---------------------------------------------------------------------------
# Cleanup and I/O errors (review item 6)
# ---------------------------------------------------------------------------


@requires_procfs
def test_group_is_killed_when_collecting_raises_an_ordinary_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-cancellation exception from ``_collect`` must still reap the worker's group and
    come back as a terminal (run() never raises)."""
    capture = tmp_path / "descendant.pid"
    fake = make_fake_opencode(
        tmp_path,
        "import signal\n"
        f"capture = {str(capture)!r}\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    with open(capture + '.tmp', 'w') as fh:\n"
        "        fh.write(str(os.getpid()))\n"
        "    os.replace(capture + '.tmp', capture)\n"
        "    while True:\n"
        "        time.sleep(0.05)\n"
        "time.sleep(120)\n",
    )

    async def exploding_collect(self: object, proc: object, timeout_seconds: float) -> object:
        for _ in range(1200):  # the worker must be running before we fail
            if capture.exists():
                break
            await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    monkeypatch.setattr(OpenCodeExecutor, "_collect", exploding_collect)
    cwd = tmp_path / "work"
    cwd.mkdir()
    try:
        terminal = run_executor(
            OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=120.0),
            "task",
            cwd,
            timeout=120.0,
        )
        assert terminal.ok is False
        assert terminal.error.startswith("worker_io_failed"), terminal.error
        descendant = int(capture.read_text())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _pid_gone(descendant):
            time.sleep(0.05)
        assert _pid_gone(descendant), "worker group survived a collect failure"
    finally:
        recorded = capture.read_text().strip() if capture.exists() else ""
        if recorded.isdigit():
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(int(recorded), signal.SIGKILL)


def test_a_failed_pipe_read_is_not_reported_as_a_finished_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pump that fails while the worker exits 0 must not yield ok=True with partial usage."""
    payload = ndjson_success("done")
    fake = make_fake_opencode(
        tmp_path, f"sys.stdout.write({payload!r})\nsys.stdout.write('\\n')\nsys.exit(0)\n"
    )
    real_read = asyncio.StreamReader.read
    state = {"failed": False}

    async def failing_read(self: asyncio.StreamReader, n: int = -1) -> bytes:
        if not state["failed"]:
            state["failed"] = True
            raise OSError("pipe read failed")
        return await real_read(self, n)

    monkeypatch.setattr(asyncio.StreamReader, "read", failing_read)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False, terminal
    assert terminal.error.startswith("worker_io_failed"), terminal.error


# ---------------------------------------------------------------------------
# PINNED_VERSION sanity
# ---------------------------------------------------------------------------


def test_pinned_version_format() -> None:
    parts = PINNED_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
