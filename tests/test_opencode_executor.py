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
    PINNED_VERSION,
    TASK_FILE_NAME,
    TASK_PROMPT,
    OpenCodeExecutor,
    _attempt_session_id,
    _build_child_env,
    _kill_process_group,
    _opencode_binary,
    _parse_ndjson_output,
    _parse_status_code,
    _status_from_full_text,
    _write_attempt_config,
)

ROUTE = "cc/claude-sonnet-5"
#: The instructions file the task is delivered in, beside the per-attempt ``opencode.json``.
TASK_FILE = "verdict-task.md"

# The real pre-dispatch functions, captured before the autouse fixture replaces them.
_REAL_SERVE_TOKEN = executor_module._serve_token
_REAL_LOAD_KEY = executor_module.load_assignment_key
_REAL_CHECK_BINARY = executor_module._check_binary
_REAL_CHECK_SERVE = executor_module._check_serve

#: The signing key every test here uses unless it tests the key itself, and the ids the helper
#: passes: the runtime gives an executor both on every worker attempt.
SIGNING_KEY = bytes(range(32))
RUN_ID = "run-1"
NODE_ID = "node-1"


@pytest.fixture(autouse=True)
def _serve_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default for every test here: a serve token and a signing key resolve, the binary runs,
    serve accepts the token.

    The fake workers are scripts that record what they are given, so the real binary check
    (which runs ``--version``) is replaced too.  The tests of the pre-dispatch checks put the
    real functions back.  Nothing here ever reads a real credential store, signing key or
    receipt database, or contacts a real serve.
    """
    monkeypatch.setattr(executor_module, "_serve_token", lambda *a, **k: "fixture-serve-token-0000")
    monkeypatch.setattr(executor_module, "load_assignment_key", lambda *a, **k: SIGNING_KEY)
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
    return asyncio.run(
        executor.run(
            prompt, route_id=ROUTE, cwd=cwd, timeout_seconds=timeout, run_id=RUN_ID, node_id=NODE_ID
        )
    )


# Event shapes below are the ones the installed OpenCode 2.3.2 prints, taken from the captures in
# ``tests/fixtures/opencode_stdout`` (see its README): ``type``, ``timestamp`` and ``sessionID``
# are top-level; the text, stop reason, tokens and cost of a step sit under ``part``.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opencode_stdout"
_TS = 1795550120185


def captured(name: str) -> str:
    """The stdout OpenCode 2.3.2 really printed (see the fixture README)."""
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _part_event(
    event_type: str, part: object, session_id: str = "ses_abc123", **top: object
) -> str:
    event: dict[str, object] = {
        "type": event_type,
        "timestamp": _TS,
        "sessionID": session_id,
        "part": part,
    }
    event.update(top)
    return json.dumps(event)


def text_event(text: str, session_id: str = "ses_abc123") -> str:
    """One whole text block, as 2.3.2 prints it."""
    part = {"type": "text", "text": text, "time": {"start": _TS - 15, "end": _TS}}
    return _part_event("text", part, session_id)


def step_finish_event(
    cost: object = 0,
    tokens_in: int = 100,
    tokens_out: int = 42,
    reason: str = "stop",
    session_id: str = "ses_abc123",
) -> str:
    """A ``step_finish`` as 2.3.2 prints it; ``cost=None`` leaves the figure out."""
    part: dict[str, object] = {
        "type": "step-finish",
        "reason": reason,
        "tokens": {
            "input": tokens_in,
            "output": tokens_out,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
    }
    if cost is not None:
        part["cost"] = cost
    return _part_event("step_finish", part, session_id)


def ndjson_success(text: str = "Done", session_id: str = "ses_abc123") -> str:
    """A one-step success run in the shape 2.3.2 prints (cost 0, as for a custom provider)."""
    lines = [
        _part_event("step_start", {"type": "step-start"}, session_id),
        text_event(text, session_id),
        step_finish_event(session_id=session_id),
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


def test_write_attempt_config_sends_no_worker_header_and_reads_the_key_from_the_environment(
    tmp_path: Path,
) -> None:
    """``X-Verdict-Worker`` selects nothing on serve any more: the signed assignment, read from
    the environment variable the config names, is the only thing that identifies the worker."""
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
    cfg = json.loads(text)
    assert "headers" not in cfg["providers"]["verdict"]
    assert "x-verdict-worker" not in text.lower()
    assert cfg["providers"]["verdict"]["settings"]["apiKey"] == "{env:VERDICT_WORKER_API_KEY}"


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


def test_write_attempt_config_lists_the_task_file_in_the_config_directory(tmp_path: Path) -> None:
    """The task is an instructions file beside ``opencode.json``, never a path in the worktree."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    config_dir = tmp_path / "cfg"
    _write_attempt_config(
        config_dir,
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((config_dir / "opencode.json").read_text())
    assert cfg["instructions"] == [str(config_dir / TASK_FILE_NAME)]
    assert not any(str(cwd) in entry for entry in cfg["instructions"])


def test_write_attempt_config_keeps_the_worktrees_agents_md_in_the_instructions(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("conventions\n")
    config_dir = tmp_path / "cfg"
    _write_attempt_config(
        config_dir,
        route_id=ROUTE,
        serve_base_url="http://127.0.0.1:8000",
        api_key_env="VERDICT_WORKER_API_KEY",
        cwd=cwd,
    )
    cfg = json.loads((config_dir / "opencode.json").read_text())
    assert cfg["instructions"] == [str(config_dir / TASK_FILE_NAME), str(cwd / "AGENTS.md")]


def test_the_task_file_name_and_the_ask_are_what_the_tests_expect() -> None:
    assert TASK_FILE_NAME == TASK_FILE
    assert 0 < len(TASK_PROMPT) <= 600


# ---------------------------------------------------------------------------
# The worktree is never written to
# ---------------------------------------------------------------------------


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


# --- lines captured from the real OpenCode 2.3.2 (tests/fixtures/opencode_stdout) -------------


def test_captured_success_run_parses_to_text_tokens_session_and_stop_reason() -> None:
    parsed = _parse_ndjson_output(captured("v2_3_2_success.jsonl"))
    assert parsed["last_text"] == "ok"
    assert parsed["session_id"] == "ses_a1b2c3d4e5f60718293a"
    assert parsed["stop_reason"] == "stop"
    usage = parsed["usage"]
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (1, 1)
    # The tool printed ``"cost":0`` for the custom provider: unknown, never free.
    assert usage.cost_usd is None and usage.cost_note and "0" in usage.cost_note
    assert parsed["error_message"] == "" and parsed["error_status"] is None


def test_captured_multistep_run_sums_its_steps_and_keeps_every_text_block() -> None:
    """Two steps (a tool call, then the answer): tokens add up, the tool_use line is not text,
    and the answer arrives as one whole block, not as the six deltas the endpoint streamed."""
    parsed = _parse_ndjson_output(captured("v2_3_2_multistep.jsonl"))
    assert parsed["last_text"] == (
        "Running a command. The quick brown fox jumps over the lazy dog. Second sentence ends here."
    )
    assert parsed["stop_reason"] == "stop", "the last step decides, not the tool-calls step"
    usage = parsed["usage"]
    assert (usage.input_tokens, usage.output_tokens) == (11 + 12, 5 + 34)
    assert usage.cost_usd is None and usage.cost_note


@pytest.mark.parametrize(
    ("name", "status", "retry_seconds", "message"),
    [
        ("v2_3_2_error.jsonl", 429, 7.0, "fake upstream 429"),
        ("v2_3_2_error_503.jsonl", 503, None, "fake upstream 503"),
    ],
)
def test_captured_upstream_failure_keeps_its_status_and_retry_delay(
    name: str, status: int, retry_seconds: float | None, message: str
) -> None:
    """The final ``error`` event decides; the ten ``retry`` events before it carry the same
    error but are not errors of the attempt."""
    parsed = _parse_ndjson_output(captured(name))
    assert parsed["error_status"] == status
    assert parsed["retry_after_seconds"] == retry_seconds
    assert parsed["error_message"] == message
    assert parsed["session_id"] == "ses_a1b2c3d4e5f60718293a"
    assert parsed["usage"] is None and parsed["last_text"] == ""


def test_executor_turns_the_captured_success_run_into_a_non_empty_ok_terminal(
    tmp_path: Path,
) -> None:
    """Regression for #1337: a real successful run used to parse to empty output and no usage,
    which recovery then classified as ``empty_output``."""
    payload = captured("v2_3_2_success.jsonl")
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({payload!r})\nsys.exit(0)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is True, terminal  # type: ignore[attr-defined]
    assert terminal.output == "ok"  # type: ignore[attr-defined]
    assert terminal.session_ref == "ses_a1b2c3d4e5f60718293a"  # type: ignore[attr-defined]
    usage = terminal.usage  # type: ignore[attr-defined]
    assert (usage.input_tokens, usage.output_tokens) == (1, 1)


@pytest.mark.parametrize(
    ("name", "status", "retry_seconds"),
    [("v2_3_2_error.jsonl", 429, 7.0), ("v2_3_2_error_503.jsonl", 503, None)],
)
def test_executor_classifies_a_captured_upstream_failure(
    tmp_path: Path, name: str, status: int, retry_seconds: float | None
) -> None:
    payload = captured(name)
    fake = make_fake_opencode(tmp_path, f"sys.stdout.write({payload!r})\nsys.exit(1)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False  # type: ignore[attr-defined]
    assert terminal.status_code == status  # type: ignore[attr-defined]
    assert terminal.retry_after_seconds == retry_seconds  # type: ignore[attr-defined]
    assert terminal.error == f"fake upstream {status}"  # type: ignore[attr-defined]


def test_only_the_part_object_is_read_for_text_tokens_and_cost() -> None:
    """2.3.2 never prints these at the top level of an event, so they are not read there: a
    line that did would be somebody else's shape, not evidence of a result."""
    top_level = "\n".join(
        [
            json.dumps({"type": "text", "sessionID": "ses_x", "text": "from the top"}),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_x",
                    "reason": "stop",
                    "cost": 0.5,
                    "tokens": {"input": 9, "output": 9},
                }
            ),
        ]
    )
    parsed = _parse_ndjson_output(top_level)
    assert parsed["last_text"] == ""
    assert parsed["usage"] is None and parsed["stop_reason"] is None


@pytest.mark.parametrize("part", [None, "text", ["text"], 7, {}, {"text": 5}, {"text": None}])
def test_a_text_or_step_finish_part_of_the_wrong_shape_is_ignored(part: object) -> None:
    lines = "\n".join(
        [_part_event("text", part), _part_event("step_finish", part), text_event("kept")]
    )
    parsed = _parse_ndjson_output(lines)
    assert parsed["last_text"] == "kept"
    assert parsed["usage"] is None and parsed["stop_reason"] is None


def test_a_secret_split_across_two_text_events_is_redacted_after_joining() -> None:
    secret = "sk-test-SPLIT-7c1f09ab"
    half = len(secret) // 2
    stream = "\n".join(
        [
            text_event(f"key is {secret[:half]}"),
            _part_event("tool_use", {"type": "tool", "tool": "bash"}),
            text_event(f"{secret[half:]} and more"),
            step_finish_event(),
        ]
    )
    parsed = _parse_ndjson_output(stream, [secret])
    assert secret not in parsed["last_text"]
    assert "[REDACTED]" in parsed["last_text"]
    assert parsed["last_text"].endswith(" and more")


def test_a_secret_in_a_whole_text_block_and_a_bearer_value_are_redacted() -> None:
    stream = text_event(f"echo {_KEY} / Authorization: Bearer {_OTHER_BEARER}")
    parsed = _parse_ndjson_output(stream, [_KEY])
    assert _KEY not in parsed["last_text"] and _OTHER_BEARER not in parsed["last_text"]
    assert parsed["last_text"].count("[REDACTED]") == 2


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
    txt = text_event(f"echo {_KEY} / Authorization: Bearer {_OTHER_BEARER}")
    stderr_text = (
        f"debug headers: Authorization: Bearer {_KEY}\n"
        f"retry with Authorization: Bearer {_OTHER_BEARER}\nkey={_KEY}\n"
    )
    lines = []
    if scenario == "ndjson_error":
        lines = [txt, json.dumps(err)]
    elif scenario == "text_output":
        lines = [txt, ndjson_success("done")]
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
                    executor.run(
                        "task",
                        route_id=ROUTE,
                        cwd=cwd,
                        timeout_seconds=20.0,
                        run_id=RUN_ID,
                        node_id=NODE_ID,
                    )
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


def test_the_worker_holds_only_the_signed_assignment_and_never_the_serve_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serve token is resolved by Verdict's own code (here via serve's documented
    LLMGATE_AUTH_TOKEN) for the orchestrator's readiness check alone.  The worker is handed a
    signed assignment, as VERDICT_WORKER_API_KEY and nowhere else: the serve token is not in its
    environment, arguments, stdin or the config directory's files (the config and the task
    instructions file), and the assignment is in none of those but the one variable."""
    from datetime import datetime, timezone

    from tests import worker_assignment_helpers as sign
    from verdict.worker_assignment import verify_assignment

    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", _SERVE_TOKEN)
    monkeypatch.setattr(executor_module, "_serve_token", _REAL_SERVE_TOKEN)
    capture = tmp_path / "capture.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import pathlib\n"
        f"cfg = pathlib.Path(os.environ['OPENCODE_CONFIG_DIR'])\n"
        f"files = {{p.name: p.read_text() for p in cfg.iterdir() if p.is_file()}}\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{\n"
        f"    'env': dict(os.environ), 'argv': sys.argv, 'config': files, 'stdin': sys.stdin.read(),\n"
        f"}}))\n"
        f"sys.exit(0)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "do the task", cwd)
    assert terminal.ok is True, terminal
    data = json.loads(capture.read_text())
    assert [n for n, v in data["env"].items() if _SERVE_TOKEN in v] == []
    for surface in ("argv", "config", "stdin"):
        assert _SERVE_TOKEN not in json.dumps(data[surface]), f"serve token leaked into {surface}"
    assert TASK_FILE_NAME in data["config"], "the instructions file was not among the files"

    # The worker's one credential is found by what it is (an assignment-shaped value) and its
    # name is asserted: exactly one variable holds one, and it is the one the config refers to.
    holders = {name: value for name, value in data["env"].items() if value.startswith("vwa1.")}
    assert list(holders) == ["VERDICT_WORKER_API_KEY"], list(holders)
    (token,) = holders.values()
    for surface in ("argv", "config", "stdin"):
        assert token not in json.dumps(data[surface]), f"the assignment leaked into {surface}"
    assert "{env:VERDICT_WORKER_API_KEY}" in json.dumps(data["config"])

    # What the worker holds is exactly one assignment for this route, run and node, that serve's
    # own verifier accepts, and it lives for the attempt's timeout (10 s here) plus the margin.
    verified = verify_assignment(token, SIGNING_KEY, now=datetime.now(timezone.utc))
    assert (verified.route_id, verified.run_id, verified.node_id) == (ROUTE, RUN_ID, NODE_ID)
    fields = sign.decode_payload(token)
    assert fields["exp"] - fields["iat"] == 10 + executor_module.ASSIGNMENT_MARGIN_SECONDS


def test_the_omniroute_gateway_key_is_never_the_worker_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OMNIROUTE_API_KEY", "omniroute-gateway-key-NOT-FOR-WORKERS")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "omniroute-gateway-key-NOT-FOR-WORKERS")
    assert _REAL_SERVE_TOKEN() is None


def test_missing_serve_credential_fails_closed_before_anything_is_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(executor_module, "_serve_token", _REAL_SERVE_TOKEN)
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
            serve_token_provider=lambda: None,
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


def _slow_version_script(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A binary whose ``--version`` records its pid and then waits for a release file.

    Returns (script, pid file, release file).  The wait is a shell loop, so the probe's process
    group holds more than one process.
    """
    pid_file = tmp_path / "probe.pid"
    release_file = tmp_path / "probe.release"
    script = tmp_path / "opencode-blocked-version"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ > '{pid_file}'\n"
        f"while [ ! -f '{release_file}' ]; do sleep 0.05; done\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, pid_file, release_file


def _cancel_run_during_a_blocked_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[bool, float, list[str]]:
    """Cancel ``run()`` while the real ``--version`` probe is blocked.

    Returns (probe process gone when the cancellation completed, seconds the cancellation took,
    the requests the fake serve saw during the second after the probe was released).  Releasing
    the probe afterwards stands for a slow probe that finishes late: a leaked thread would then
    go on to the authenticated serve request.
    """
    monkeypatch.setattr(executor_module, "_check_binary", _REAL_CHECK_BINARY)
    monkeypatch.setattr(executor_module, "_check_serve", _REAL_CHECK_SERVE)
    script, pid_file, release_file = _slow_version_script(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    serve = _FakeServe(accepts=_SERVE_TOKEN)
    executor = OpenCodeExecutor(
        opencode_bin=str(script),
        serve_base_url=serve.origin,
        serve_token_provider=lambda: _SERVE_TOKEN,
    )

    async def scenario() -> tuple[bool, float]:
        task = asyncio.ensure_future(
            executor.run(
                "task",
                route_id=ROUTE,
                cwd=cwd,
                timeout_seconds=60.0,
                run_id=RUN_ID,
                node_id=NODE_ID,
            )
        )
        for _ in range(500):  # up to 5 s for the probe to start
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists(), "the version probe never started"
        cancelled_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20.0)
        took = time.monotonic() - cancelled_at
        gone = _pid_gone(int(pid_file.read_text()))
        release_file.write_text("go")  # a probe that is still alive would now succeed
        await asyncio.sleep(1.0)  # time for a leaked thread to make its request
        return gone, took

    try:
        gone, took = asyncio.run(scenario())
        return gone, took, list(serve.requests)
    finally:
        serve.close()
        recorded = pid_file.read_text().strip() if pid_file.exists() else ""
        if recorded.isdigit():  # never leave the probe running, whatever failed above
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(int(recorded), signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(int(recorded), signal.SIGKILL)


@requires_procfs
def test_cancelling_run_kills_the_version_probe_and_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 384b5c82: cancelling the pre-dispatch await did not stop its thread, so the
    ``--version`` probe stayed alive after ``run()`` had finished cancelling.  The probe must be
    gone, reaped, by the time the cancellation completes, and promptly."""
    gone, took, _requests = _cancel_run_during_a_blocked_version_probe(tmp_path, monkeypatch)
    assert gone, "the version probe was still alive when the cancellation completed"
    assert took < 10.0, f"the cancellation waited {took:.1f}s for a probe it could have killed"


@requires_procfs
def test_cancelling_run_during_the_version_probe_makes_no_serve_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread of a cancelled attempt must not go on to the authenticated serve request."""
    _gone, _took, requests = _cancel_run_during_a_blocked_version_probe(tmp_path, monkeypatch)
    assert requests == [], f"a cancelled attempt still called serve: {requests}"


def test_cancelling_run_between_the_binary_and_serve_checks_skips_the_serve_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary check that cannot be interrupted (it returns normally after the cancel) is still
    followed by no serve request: the thread looks at the cancellation before it starts one."""
    import threading

    started, release = threading.Event(), threading.Event()
    serve_calls: list[str] = []

    def blocked_binary_check(*args: object, **kwargs: object) -> None:
        started.set()
        assert release.wait(timeout=30), "test never released the blocked binary check"

    def recording_serve_check(origin: str, token: str, timeout: float) -> None:
        serve_calls.append(origin)

    monkeypatch.setattr(executor_module, "_check_binary", blocked_binary_check)
    monkeypatch.setattr(executor_module, "_check_serve", recording_serve_check)
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=make_fake_opencode(tmp_path, "sys.exit(0)\n"))

    async def scenario() -> None:
        task = asyncio.ensure_future(
            executor.run(
                "task",
                route_id=ROUTE,
                cwd=cwd,
                timeout_seconds=30.0,
                run_id=RUN_ID,
                node_id=NODE_ID,
            )
        )
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the binary check never started"
        task.cancel()
        await asyncio.sleep(0.2)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20.0)
        await asyncio.sleep(0.3)  # time for a leaked thread to reach the serve check

    asyncio.run(scenario())
    assert serve_calls == [], "a serve request was made after the attempt was cancelled"
    assert list(cwd.iterdir()) == [], "the worktree was written to after the cancellation"


def test_cancelling_run_waits_for_a_serve_check_that_is_already_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that has started cannot be recalled; the cancellation completes only after it
    has returned, so nothing of the attempt is left running behind it."""
    import threading

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_serve_check(origin: str, token: str, timeout: float) -> None:
        started.set()
        assert release.wait(timeout=30), "test never released the blocked serve check"
        finished.set()

    monkeypatch.setattr(executor_module, "_check_serve", blocked_serve_check)
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=make_fake_opencode(tmp_path, "sys.exit(0)\n"))

    async def scenario() -> None:
        task = asyncio.ensure_future(
            executor.run(
                "task",
                route_id=ROUTE,
                cwd=cwd,
                timeout_seconds=30.0,
                run_id=RUN_ID,
                node_id=NODE_ID,
            )
        )
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the serve check never started"
        task.cancel()
        await asyncio.sleep(0.3)
        assert not task.done(), "run() finished cancelling while its serve check was running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20.0)
        assert finished.is_set(), "the cancellation completed before the serve check returned"

    asyncio.run(scenario())
    assert list(cwd.iterdir()) == [], "the worktree was written to after the cancellation"


def test_a_failing_credential_lookup_is_a_configuration_failure_not_an_exception(
    tmp_path: Path,
) -> None:
    def exploding() -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "malformed credentials file")

    fake = make_fake_opencode(tmp_path, "sys.exit(0)\n")
    terminal = run_executor(
        OpenCodeExecutor(opencode_bin=fake, serve_token_provider=exploding), "task", tmp_path
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
            serve_token_provider=lambda: _SERVE_TOKEN,
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


def test_the_assignment_never_reaches_terminal_events_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """The worker echoes the credential it was given (from its env) to stderr and into NDJSON:
    the assignment, its payload and its tag are redacted wherever the executor records output."""
    seen = tmp_path / "assignment.txt"
    fake = make_fake_opencode(
        tmp_path,
        "tok = os.environ['VERDICT_WORKER_API_KEY']\n"
        f"open({str(seen)!r}, 'w').write(tok)\n"
        "payload, tag = tok.split('.')[1:]\n"
        "sys.stderr.write('debug Authorization: Bearer ' + tok + ' key=' + tok + ' tag=' + tag + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'error', 'error': {'message': 'bad ' + tok + ' ' + payload}}) + '\\n')\n"
        "sys.exit(1)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    with caplog.at_level(logging.DEBUG):
        terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    token = seen.read_text()
    payload, tag = token.split(".")[1:]
    recorded = json.dumps(dataclasses.asdict(terminal), default=str)
    captured = capfd.readouterr()
    for secret in (token, payload, tag, "fixture-serve-token-0000"):
        assert secret not in recorded
        assert secret not in caplog.text
        assert secret not in captured.out + captured.err
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
# Dispatch origin (#1326): the executor reports where its worker really sends
# ---------------------------------------------------------------------------


def _reported_origin(executor: object) -> str | None:
    from verdict.orchestration.contracts import executor_gateway_origin

    return executor_gateway_origin(executor, "n", route_id=ROUTE, cwd=Path("unused"))


def test_the_executor_reports_its_real_serve_origin_not_the_local_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.orchestration import contracts

    monkeypatch.delenv("VERDICT_SERVE_URL", raising=False)
    default = _reported_origin(OpenCodeExecutor())
    assert default == "http://127.0.0.1:8000"
    assert default != contracts.LOCAL_EXECUTOR_ORIGIN
    # It is reported as a serve origin, the one kind the runtime binds to the worker serve address.
    assert isinstance(default, contracts.WorkerServeOrigin)
    # Resolved the way the executor resolves its serve address: the variable, then the flag.
    monkeypatch.setenv("VERDICT_SERVE_URL", "http://127.0.0.1:9123/v1")
    assert _reported_origin(OpenCodeExecutor()) == "http://127.0.0.1:9123"
    assert _reported_origin(OpenCodeExecutor(serve_base_url="http://localhost:9555")) == (
        "http://localhost:9555"
    )


def test_the_reported_origin_is_the_one_the_worker_calls(tmp_path: Path) -> None:
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
    executor = OpenCodeExecutor(opencode_bin=fake, serve_base_url="http://127.0.0.1:9123")
    reported = _reported_origin(executor)
    run_executor(executor, "t", cwd)
    config = json.loads(capture.read_text())
    assert config["providers"]["verdict"]["settings"]["baseURL"] == f"{reported}/v1"


def test_the_local_origin_declaration_and_its_stopgap_note_are_gone() -> None:
    source = Path(executor_module.__file__).read_text()
    assert not hasattr(executor_module, "LOCAL_EXECUTOR_ORIGIN")
    assert "LOCAL_EXECUTOR_ORIGIN" not in source
    assert "DISPATCH_ORIGIN_CONFLICT" not in source


# ---------------------------------------------------------------------------
# Upstream HTTP status and cost (review items 4 and 12)
# ---------------------------------------------------------------------------


_ERROR_TYPES = {429: "provider.rate-limit"}


def _ndjson_error(status: int, message: str, retry_after_ms: int | None = None) -> str:
    """A final ``error`` event as 2.3.2 prints it (see ``v2_3_2_error.jsonl``)."""
    detail: dict[str, object] = {
        "type": _ERROR_TYPES.get(status, "provider.internal"),
        "message": message,
        "status": status,
        "response": {"body": json.dumps({"error": {"message": message}})},
    }
    if retry_after_ms is not None:
        detail["retryAfterMs"] = retry_after_ms
    return json.dumps(
        {"type": "error", "timestamp": _TS, "sessionID": "ses_abc123", "error": detail}
    )


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


# Text that carries a 4xx or 5xx number and no HTTP status: a delay, a count, a size, a line.
_INCIDENTAL_NUMBERS = [
    "retrying in 500 ms",
    "failed after 429 attempts",
    "gave up after 503 retries",
    "backoff 504 seconds",
    "downloaded 404 files",
    "line 502 of config.json is invalid",
    "wrote 400 bytes then lost the connection",
    "Retry-After: 429",
    "exit code 401",
    "retry 3 of 5, waiting 500 and then 429",
    "request id 5001234 failed",
]

# Real statuses, written the ways a provider, a proxy or a CLI prints them.
_MARKED_STATUSES = [
    ("HTTP 429", 429),
    ("http 429", 429),
    ("HTTP/1.1 503 Service Unavailable", 503),
    ("HTTP/2 502", 502),
    ("HTTP error 500", 500),
    ("HTTP status 504", 504),
    ("status 503", 503),
    ("status: 504", 504),
    ("Status Code 408", 408),
    ("status code: 401", 401),
    ("status_code=403", 403),
    ("statusCode: 429", 429),
    ('{"status":429,"message":"slow down"}', 429),
    ("429 Too Many Requests", 429),
    ("upstream answered 503 Service Unavailable twice", 503),
    ("(401 Unauthorized)", 401),
    ("502 bad gateway", 502),
    # The ``requests`` library's own wording (``HTTPError`` text): the number is followed by
    # ``Client Error`` or ``Server Error``, then the reason and the url.
    ("429 Client Error: Too Many Requests for url: http://127.0.0.1:8000/v1/chat/completions", 429),
    (
        "503 Server Error: Service Unavailable for url: http://127.0.0.1:8000/v1/chat/completions",
        503,
    ),
    ("requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: http://x/y", 502),
    ("401 Client Error: Unauthorized for url: https://example.test/v1", 401),
    ("404 Client Error: Not Found for url: https://example.test/v1/models", 404),
    ("500 Server Error: Internal Server Error for url: https://example.test/v1", 500),
]

# Near misses of that wording: a count, a different noun, or the wrong class for the number.
_NOT_REQUESTS_ERRORS = [
    "500 server errors today",
    "failed after 429 client errors",
    "retrying in 500 ms",
    "failed after 429 attempts",
    "429 Server Error: Too Many Requests for url: http://x",  # requests says Client for 4xx
    "503 Client Error: Service Unavailable for url: http://x",  # and Server for 5xx
    "the 500 Server Error page",  # no colon after Error
    "1429 Client Error: not a status",
    "429 Client",
]


@pytest.mark.parametrize("text", _NOT_REQUESTS_ERRORS)
def test_near_misses_of_the_requests_wording_are_not_a_status(text: str) -> None:
    assert _parse_status_code(text) is None
    assert _status_from_full_text(text) is None
    assert _status_from_full_text(f"{text}.{_LONG_DIAGNOSTIC} {text}") is None


@pytest.mark.parametrize(
    ("text", "status", "category"),
    [
        (
            "429 Client Error: Too Many Requests for url: http://127.0.0.1:8000/v1/chat/completions",
            429,
            "rate_limited",
        ),
        (
            "503 Server Error: Service Unavailable for url: http://127.0.0.1:8000/v1/chat/completions",
            503,
            "upstream_temporary",
        ),
    ],
)
@pytest.mark.parametrize("where", ["stderr", "ndjson-message"])
def test_a_requests_error_gives_its_status_and_a_reroute(
    tmp_path: Path, where: str, text: str, status: int, category: str
) -> None:
    """End to end, as recovery sees it: the status reaches the terminal and the failure is a
    reroute (a rate limit or an upstream outage), not an unclassified one."""
    from verdict.orchestration.recovery import FailureIntelligence

    if where == "stderr":
        body = f"sys.stderr.write({text!r})\nsys.exit(1)\n"
    else:
        event = {"type": "error", "message": text}
        body = f"sys.stdout.write({json.dumps(event)!r} + '\\n')\nsys.exit(1)\n"
    fake = make_fake_opencode(tmp_path, body)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False and terminal.status_code == status  # type: ignore[attr-defined]
    failure = FailureIntelligence().classify(
        terminal,  # type: ignore[arg-type]
        now=datetime(2026, 11, 24, tzinfo=timezone.utc),
    )
    assert failure.action == "REROUTE" and failure.category == category, failure


@pytest.mark.parametrize("text", _INCIDENTAL_NUMBERS)
def test_a_number_without_an_http_or_status_marker_is_not_a_status(text: str) -> None:
    """Review of 384b5c82: ``retrying in 500 ms`` became status 500 and ``failed after 429
    attempts`` became a rate limit that recovery reroutes with a provider cooldown."""
    assert _parse_status_code(text) is None
    assert _status_from_full_text(text) is None
    assert _status_from_full_text(f"{text}.{_LONG_DIAGNOSTIC} {text}") is None


@pytest.mark.parametrize(("text", "status"), _MARKED_STATUSES)
def test_a_marked_http_status_in_unstructured_text_is_still_found(text: str, status: int) -> None:
    assert _parse_status_code(f"request failed: {text}") == status
    assert _parse_status_code(text) == status


@pytest.mark.parametrize(("text", "status"), _MARKED_STATUSES)
def test_a_marked_status_at_the_end_of_a_long_text_is_still_found(text: str, status: int) -> None:
    long_text = f"retrying in 500 ms.{_LONG_DIAGNOSTIC} fatal: {text}"
    assert len(long_text) > 400
    assert _status_from_full_text(long_text) == status


def test_a_marked_status_pushed_out_of_the_tail_is_still_found_past_an_incidental_number() -> None:
    text = f"fatal: HTTP 503 Service Unavailable.{_LONG_DIAGNOSTIC} failed after 429 attempts"
    assert _status_from_full_text(text) == 503


@pytest.mark.parametrize("where", ["stderr", "ndjson-message", "ndjson-error-string"])
def test_an_incidental_number_in_a_failed_attempt_gives_no_status(
    tmp_path: Path, where: str
) -> None:
    """End to end: the terminal of a failed attempt carries ``status_code=None`` when the only
    4xx or 5xx number in its error text is a delay or a count, so recovery does not reroute it
    as a rate limit or an outage."""
    text = f"retrying in 500 ms after failed after 429 attempts.{_LONG_DIAGNOSTIC}"
    if where == "stderr":
        body = f"sys.stderr.write({text!r})\nsys.exit(1)\n"
    else:
        event = (
            {"type": "error", "message": text}
            if where == "ndjson-message"
            else {"type": "error", "error": text}
        )
        body = f"sys.stdout.write({json.dumps(event)!r} + '\\n')\nsys.exit(1)\n"
    fake = make_fake_opencode(tmp_path, body)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.status_code is None


def test_an_incidental_number_in_the_ndjson_message_does_not_hide_a_marked_stderr_status(
    tmp_path: Path,
) -> None:
    line = json.dumps({"type": "error", "message": "retrying in 500 ms, failed after 429 attempts"})
    fake = make_fake_opencode(
        tmp_path,
        f"sys.stdout.write({line!r} + '\\n')\nsys.stderr.write('status code: 503')\nsys.exit(1)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.status_code == 503


def test_a_status_in_the_tail_still_beats_an_earlier_stray_number_in_stderr(tmp_path: Path) -> None:
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


def test_stderr_status_is_the_fallback_when_the_ndjson_error_has_none(tmp_path: Path) -> None:
    """The last open review finding of #1299: an NDJSON error with a message but no status
    used to make stderr unread, so ``HTTP 429`` there left ``status_code=None`` and recovery
    saw an unclassified failure instead of a reroutable one.  The NDJSON message is still the
    recorded error."""
    line = json.dumps({"type": "error", "message": "request failed"})
    fake = make_fake_opencode(
        tmp_path,
        f"sys.stdout.write({line!r} + '\\n')\nsys.stderr.write('HTTP 429 Too Many Requests')\n"
        "sys.exit(1)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False
    assert terminal.status_code == 429
    assert terminal.error == "request failed", "the NDJSON message stays the recorded error"


def test_a_status_in_a_long_stderr_is_found_when_the_ndjson_error_has_none(tmp_path: Path) -> None:
    """The stderr fallback reads the whole sanitized text, as it does without an NDJSON error."""
    line = json.dumps({"type": "error", "message": "request failed"})
    text = f"fatal: HTTP 503 Service Unavailable.{_LONG_DIAGNOSTIC}"
    fake = make_fake_opencode(
        tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.stderr.write({text!r})\nsys.exit(1)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.status_code == 503
    assert terminal.error == "request failed"


def test_a_structured_status_beats_stderr_when_both_exist(tmp_path: Path) -> None:
    """Stderr is only the fallback: the status the structured error carries is not replaced."""
    line = _ndjson_error(503, "upstream said no")
    fake = make_fake_opencode(
        tmp_path,
        f"sys.stdout.write({line!r} + '\\n')\nsys.stderr.write('HTTP 429 Too Many Requests')\n"
        "sys.exit(1)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.status_code == 503
    assert terminal.error == "upstream said no"


def test_a_secret_in_stderr_is_redacted_before_a_status_is_read_from_it(tmp_path: Path) -> None:
    """With an NDJSON error present, stderr is still sanitized first: the digits of a secret
    that happens to look like a status are not a status, and the secret is not recorded."""
    secret = "sk-test-503-STDERRFALLBACK"
    line = json.dumps({"type": "error", "message": "request failed"})
    fake = make_fake_opencode(
        tmp_path,
        f"sys.stdout.write({line!r} + '\\n')\nsys.stderr.write('key=' + {secret!r})\nsys.exit(1)\n",
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, serve_token_provider=lambda: secret)
    terminal = run_executor(executor, "task", cwd)
    assert terminal.status_code is None
    assert terminal.error == "request failed"
    assert secret not in json.dumps(dataclasses.asdict(terminal), default=str)


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
    executor = OpenCodeExecutor(opencode_bin=fake, serve_token_provider=lambda: secret)
    terminal = run_executor(executor, "task", cwd)
    assert terminal.status_code == 429
    assert secret not in json.dumps(dataclasses.asdict(terminal), default=str)


# ---------------------------------------------------------------------------
# Malformed worker output and credentials never make run() raise (review of 13124378)
# ---------------------------------------------------------------------------


def test_deeply_nested_json_in_worker_output_is_malformed_not_a_crash() -> None:
    """``json.loads`` raises RecursionError (not ValueError) on thousands of nested arrays, and a
    worker can print such a line well within the output cap."""
    parsed = _parse_ndjson_output("[" * 100_000 + "\n" + text_event("ok"))
    assert parsed["last_text"] == "ok", "the lines after the bad one must still be read"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_worker_that_prints_deeply_nested_json_still_gets_a_terminal(
    tmp_path: Path, exit_code: int
) -> None:
    fake = make_fake_opencode(
        tmp_path, f"sys.stdout.write('[' * 100000 + '\\n')\nsys.exit({exit_code})\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is (exit_code == 0)
    assert terminal.harness == "opencode"


def test_a_retry_delay_belongs_to_the_error_event_whose_message_is_kept() -> None:
    """An earlier 429 with a one-hour retry delay must not stay attached to a later 503 that
    has none: recovery schedules a cooldown from the delay."""
    first = _ndjson_error(429, "rate limited", retry_after_ms=3_600_000)
    second = _ndjson_error(503, "unavailable")
    parsed = _parse_ndjson_output(first + "\n" + second)
    assert parsed["error_status"] == 503
    assert parsed["error_message"] == "unavailable"
    assert parsed["retry_after_seconds"] is None


def test_a_later_error_with_its_own_retry_delay_uses_it() -> None:
    first = _ndjson_error(429, "rate limited", retry_after_ms=3_600_000)
    second = _ndjson_error(429, "rate limited again", retry_after_ms=7000)
    assert _parse_ndjson_output(first + "\n" + second)["retry_after_seconds"] == 7.0


def test_a_later_error_event_without_a_message_replaces_all_three_not_only_two() -> None:
    """The final error event decides message, status and retry delay together.  A later event
    that carries a status and a delay but no message must not leave the first event's message
    attached to them."""
    first = _ndjson_error(429, "rate limited", retry_after_ms=3_600_000)
    second = json.dumps({"type": "error", "error": {"status": 503, "retryAfterMs": 9000}})
    parsed = _parse_ndjson_output(first + "\n" + second)
    assert parsed["error_message"] == ""
    assert parsed["error_status"] == 503
    assert parsed["retry_after_seconds"] == 9.0
    delay_only = json.dumps({"type": "error", "error": {"retryAfterMs": 9000}})
    parsed = _parse_ndjson_output(first + "\n" + delay_only)
    assert (parsed["error_message"], parsed["error_status"]) == ("", None)
    assert parsed["retry_after_seconds"] == 9.0


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error"},
        {"type": "error", "error": {}},
        {"type": "error", "error": {"message": ""}},
        {"type": "error", "error": "   "},
    ],
    ids=["bare", "empty-object", "empty-message", "blank-string"],
)
def test_an_error_event_with_nothing_to_say_is_ignored(event: dict[str, object]) -> None:
    first = _ndjson_error(429, "rate limited", retry_after_ms=3_600_000)
    parsed = _parse_ndjson_output(first + "\n" + json.dumps(event))
    assert parsed["error_message"] == "rate limited"
    assert parsed["error_status"] == 429
    assert parsed["retry_after_seconds"] == 3600.0


@pytest.mark.parametrize("message", ["", 123, None, ["x"]], ids=["empty", "int", "null", "list"])
def test_a_status_without_a_usable_message_is_kept_on_its_own(message: object) -> None:
    line = json.dumps({"type": "error", "error": {"message": message, "status": 502}})
    parsed = _parse_ndjson_output(line)
    assert parsed["error_message"] == ""
    assert parsed["error_status"] == 502


@pytest.mark.parametrize(
    "token",
    ["tok\nen", "tok\r\nen", "tok\x00en", "tok\x7fen", "tok\u20acen"],
    ids=["newline", "crlf", "nul", "del", "not-latin-1"],
)
def test_a_malformed_serve_credential_is_a_configuration_failure_not_an_exception(
    tmp_path: Path, token: str
) -> None:
    """A token an HTTP header cannot carry (an embedded newline, say) made urllib raise
    ValueError out of the readiness check and out of run().  It is one harness_configuration
    failure, with no worker launched and the token never echoed."""
    ran = tmp_path / "worker-ran.txt"
    fake = make_fake_opencode(
        tmp_path, f"import pathlib\npathlib.Path({str(ran)!r}).write_text('ran')\nsys.exit(0)\n"
    )
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor_module._check_serve = _REAL_CHECK_SERVE  # restored by the autouse fixture
    executor = OpenCodeExecutor(
        opencode_bin=fake, serve_base_url="http://127.0.0.1:9", serve_token_provider=lambda: token
    )
    terminal = run_executor(executor, "task", cwd)
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration:"), terminal.error
    assert token not in terminal.error and token.strip() not in terminal.error
    assert not ran.exists()


def test_the_serve_readiness_check_itself_never_raises_on_a_bad_header_value() -> None:
    reason = _REAL_CHECK_SERVE("http://127.0.0.1:9", "tok\u20acen", 1.0)
    assert reason is not None and "tok" not in reason


def test_an_unexpected_exception_inside_run_is_a_failed_terminal_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run()`` promises never to raise: whatever else goes wrong after the worker has run is
    one failed terminal that names the exception type (never its message) and leaves nothing
    behind."""
    roots: list[str] = []
    fake = make_fake_opencode(
        tmp_path,
        "import os\nopen(os.environ['XDG_DATA_HOME'] + '.marker', 'w').write('x')\nsys.exit(0)\n",
    )

    def boom(self: object, **kwargs: object) -> object:
        raise RuntimeError("internal detail containing fixture-serve-token-0000")

    real_write = executor_module._write_attempt_config

    def spying(config_dir: Path, **kwargs: object) -> None:
        roots.append(str(config_dir.parent))
        real_write(config_dir, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(OpenCodeExecutor, "_interpret", boom)
    monkeypatch.setattr(executor_module, "_write_attempt_config", spying)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "task", cwd)
    assert terminal.ok is False and terminal.harness == "opencode"
    assert terminal.error == "worker_error: RuntimeError"
    assert "fixture-serve-token-0000" not in terminal.error
    assert len(roots) == 1 and not Path(roots[0]).exists(), "the attempt directory was left behind"


def _step_finish(cost: object, tokens_in: int = 100, tokens_out: int = 42) -> str:
    return step_finish_event(cost, tokens_in, tokens_out)


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
# Task delivery (#1327): an instructions file in the attempt's config directory and one short
# prompt on stdin; nothing is written into the worktree
# ---------------------------------------------------------------------------

#: A marker that only the task text carries: how many times it reaches OpenCode is the check.
TASK_MARK = "TASKMARK-9f3a1c"


def _recording_worker(tmp_path: Path, behaviour: str = "") -> tuple[str, Path]:
    """A fake opencode that records everything it is handed, then behaves as ``behaviour`` says.

    The record holds its argv, all of its stdin, the files of its config directory (read at
    launch, so a file that is missing then is missing here) and a listing of its working
    directory.  ``behaviour`` is Python run after the record is written (default: succeed).
    """
    capture = tmp_path / "delivery.json"
    payload = ndjson_success("done")
    body = (
        "import pathlib\n"
        "cfg = pathlib.Path(os.environ['OPENCODE_CONFIG_DIR'])\n"
        "files = {str(p.relative_to(cfg)): p.read_text(errors='replace')\n"
        "         for p in sorted(cfg.rglob('*')) if p.is_file()}\n"
        "record = {'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'config_dir': str(cfg),\n"
        "          'files': files, 'cwd_listing': sorted(os.listdir(os.getcwd()))}\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(record))\n"
        + (behaviour or f"sys.stdout.write({payload!r} + '\\n')\nsys.exit(0)\n")
    )
    return make_fake_opencode(tmp_path, body), capture


def _deliver(tmp_path: Path, task: str, **kwargs: object) -> tuple[dict[str, object], object]:
    """Run ``task`` through a recording worker; return what it was handed and the terminal."""
    fake, capture = _recording_worker(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), task, cwd, **kwargs)  # type: ignore[arg-type]
    assert capture.exists(), f"the worker never ran: {terminal}"
    return json.loads(capture.read_text()), terminal


def test_the_task_is_an_instructions_file_in_the_attempt_config_directory(tmp_path: Path) -> None:
    """The file exists when the worker launches, holds the task, and is the entry the
    per-attempt ``opencode.json`` lists (OpenCode skips a listed file that is missing, so the
    path the worker is told to load and the path that was written must be the same one)."""
    record, terminal = _deliver(tmp_path, f"Implement the feature. {TASK_MARK}")
    assert terminal.ok is True  # type: ignore[attr-defined]
    files = record["files"]
    assert TASK_FILE in files, (
        f"no instructions file at launch; the config dir held {sorted(files)}"
    )
    assert TASK_MARK in files[TASK_FILE]
    config = json.loads(files["opencode.json"])
    listed = Path(str(record["config_dir"])) / TASK_FILE
    assert str(listed) in config["instructions"]


def test_the_prompt_is_one_short_ask_that_carries_no_task_text(tmp_path: Path) -> None:
    record, _ = _deliver(tmp_path, f"Implement the feature. {TASK_MARK}")
    argv = [str(a) for a in record["argv"]]  # type: ignore[attr-defined]
    ask = str(record["stdin"])
    assert ask.strip(), "no prompt reached the worker's stdin"
    assert len(ask) <= 600, f"the prompt is not short ({len(ask)} characters)"
    assert TASK_MARK not in ask and not any(TASK_MARK in arg for arg in argv)
    assert "--" not in argv, "the prompt must not also travel as an argument"
    assert not any(arg == ask or ask in arg for arg in argv), "the prompt was sent twice"


def test_the_ask_is_the_same_for_every_task(tmp_path: Path) -> None:
    first, _ = _deliver(tmp_path, "Task one: rename a variable.")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second, _ = _deliver(second_dir, "Task two: " + "x" * 5000)
    assert first["stdin"] and first["stdin"] == second["stdin"]


def test_the_task_text_reaches_opencode_exactly_once(tmp_path: Path) -> None:
    """Sending it as the prompt too doubled those input tokens on every model call (#1299)."""
    record, _ = _deliver(tmp_path, f"Implement the feature. {TASK_MARK}")
    everything = json.dumps(record["argv"]) + str(record["stdin"]) + json.dumps(record["files"])
    assert everything.count(TASK_MARK) == 1


def test_a_task_larger_than_one_argument_is_delivered_whole(tmp_path: Path) -> None:
    """Linux refuses one argument over 131072 bytes; the task no longer travels as one."""
    task = f"{TASK_MARK} " + "pack line of the context\n" * 12_000  # about 300 KB
    assert len(task.encode()) > 131_072
    record, terminal = _deliver(tmp_path, task)
    assert terminal.ok is True  # type: ignore[attr-defined]
    delivered = record["files"][TASK_FILE]  # type: ignore[index]
    assert task in delivered and len(delivered) < len(task) + 1000


def test_the_title_is_given_so_opencode_makes_no_second_request(tmp_path: Path) -> None:
    """Without ``--title`` OpenCode 2.3.2 makes a second, title-agent request on the route."""
    record, _ = _deliver(tmp_path, "task")
    argv = [str(a) for a in record["argv"]]  # type: ignore[attr-defined]
    assert "--title" in argv
    title = argv[argv.index("--title") + 1]
    assert title.strip() and not title.startswith("-")
    session = argv[argv.index("--session") + 1]
    assert session in title


@pytest.mark.parametrize("how", ["success", "nonzero-exit", "spawn-failure"])
def test_nothing_is_written_into_the_worktree(tmp_path: Path, how: str) -> None:
    """The worktree is the repository: the worker's own edits are all that may appear in it."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "tracked.txt").write_text("content\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    before = sorted(p.name for p in work.iterdir())
    if how == "spawn-failure":
        bin_path = "/nonexistent/path/to/opencode-xyz"
        record = None
    else:
        behaviour = "sys.exit(1)\n" if how == "nonzero-exit" else ""
        bin_path, capture = _recording_worker(tmp_path, behaviour)
        record = capture
    terminal = run_executor(OpenCodeExecutor(opencode_bin=bin_path), "the task", work)
    assert terminal.ok is (how == "success")  # type: ignore[attr-defined]
    assert sorted(p.name for p in work.iterdir()) == before
    assert _git(work, "status", "--porcelain") == ""
    if record is not None:
        launched = json.loads(record.read_text())
        assert launched["cwd_listing"] == [".git", "tracked.txt"], launched["cwd_listing"]


@pytest.mark.parametrize("how", ["success", "nonzero-exit", "timeout", "stalled", "spawn-failure"])
def test_the_instructions_file_is_removed_with_the_attempt_directory_on_every_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Every attempt directory lives under one temp root here, so a leftover is visible."""
    import tempfile

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    behaviours = {
        "success": "",
        "nonzero-exit": "sys.exit(1)\n",
        "timeout": "time.sleep(60)\n",
        "stalled": "time.sleep(60)\n",
    }
    if how == "spawn-failure":
        bin_path, capture = "/nonexistent/path/to/opencode-xyz", None
    else:
        bin_path, capture = _recording_worker(tmp_path, behaviours[how])
    work = tmp_path / "work"
    work.mkdir()
    executor = OpenCodeExecutor(
        opencode_bin=bin_path, idle_output_seconds=1.0 if how == "stalled" else 120.0
    )
    run_executor(executor, f"the task {TASK_MARK}", work, timeout=1.5 if how == "timeout" else 30.0)
    if capture is not None:
        launched = json.loads(capture.read_text())
        assert TASK_FILE in launched["files"], "the instructions file was not there at launch"
        assert not Path(launched["config_dir"]).exists()
    assert list(temp_root.iterdir()) == [], "the per-attempt directory outlived the attempt"


def test_the_instructions_file_is_removed_when_the_attempt_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    bin_path, capture = _recording_worker(tmp_path, "time.sleep(60)\n")
    work = tmp_path / "work"
    work.mkdir()
    executor = OpenCodeExecutor(opencode_bin=bin_path, idle_output_seconds=120.0)

    async def scenario() -> None:
        task = asyncio.ensure_future(
            executor.run(
                "the task",
                route_id=ROUTE,
                cwd=work,
                timeout_seconds=60.0,
                run_id=RUN_ID,
                node_id=NODE_ID,
            )
        )
        for _ in range(500):  # up to 5 s for the worker to launch and record
            if capture.exists():
                break
            await asyncio.sleep(0.01)
        assert capture.exists(), "the worker never launched"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30.0)

    asyncio.run(scenario())
    assert TASK_FILE in json.loads(capture.read_text())["files"]
    assert list(temp_root.iterdir()) == [], "the per-attempt directory outlived the cancellation"


def _write_nothing(config_dir: Path, task: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)


def _write_empty(config_dir: Path, task: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / TASK_FILE).write_text("")


def _write_a_directory(config_dir: Path, task: str) -> None:
    (config_dir / TASK_FILE).mkdir(parents=True, exist_ok=True)


def _write_unreadable(config_dir: Path, task: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / TASK_FILE
    target.write_text(task)
    target.chmod(0)


@pytest.mark.parametrize(
    "writer",
    [_write_nothing, _write_empty, _write_a_directory, _write_unreadable],
    ids=["missing", "empty", "a-directory", "unreadable"],
)
def test_a_missing_or_unusable_instructions_file_fails_the_attempt_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: object
) -> None:
    """OpenCode skips a listed instructions file that is not there and carries on, so the worker
    would run without its task.  The adapter must catch that before it launches anything."""
    import tempfile

    if writer is _write_unreadable and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    monkeypatch.setattr(executor_module, "_write_task_file", writer)
    fake, marker = _spawn_recorder(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "the task", work)
    assert terminal.ok is False  # type: ignore[attr-defined]
    assert terminal.error.startswith("config_write_failed:"), terminal.error  # type: ignore[attr-defined]
    assert TASK_FILE in terminal.error  # type: ignore[attr-defined]
    assert not marker.exists(), "the worker was launched without its task"
    assert list(temp_root.iterdir()) == [], "the per-attempt directory outlived the attempt"
    assert list(work.iterdir()) == []


def test_the_ask_is_sent_even_when_the_worker_has_already_gone() -> None:
    """A process that exited before the ask was written is a broken pipe, not a crash."""

    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass", stdin=asyncio.subprocess.PIPE
        )
        await proc.wait()
        await executor_module._send_prompt(proc, "the ask")  # type: ignore[attr-defined]

    asyncio.run(scenario())


@pytest.mark.parametrize("symlink", [True, False], ids=["tracked-symlink", "tracked-file"])
def test_a_repository_that_tracks_a_file_with_the_old_context_name_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink: bool
) -> None:
    """Older versions refused to start in such a repository (they wrote that name there).  Now
    nothing is written into the worktree, and git is not asked anything, so the attempt runs and
    the tracked entry, the index and the symlink's outside target are exactly as they were."""
    outside = tmp_path / "outside-target.txt"
    outside.write_text("ORIGINAL OUTSIDE CONTENT")
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    old_name = repo / ".verdict-context.md"
    if symlink:
        old_name.symlink_to(outside)
    else:
        old_name.write_text("tracked content\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / ".git" / "index").write_bytes(b"not a git index")  # git itself could not answer now

    def no_git(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"the executor ran a subprocess through subprocess.run: {args}")

    monkeypatch.setattr("verdict.orchestration.executor_opencode.subprocess.run", no_git)
    fake, capture = _recording_worker(tmp_path)
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "the task", repo)
    assert terminal.ok is True, terminal  # type: ignore[attr-defined]
    assert outside.read_text() == "ORIGINAL OUTSIDE CONTENT"
    assert old_name.is_symlink() is symlink
    if not symlink:
        assert old_name.read_text() == "tracked content\n"
    launched = json.loads(capture.read_text())
    assert launched["cwd_listing"] == [".git", ".verdict-context.md"]


def test_a_task_that_cannot_be_encoded_fails_closed_before_launch(tmp_path: Path) -> None:
    """A lone surrogate cannot be written as UTF-8: a failed attempt, not an exception."""
    fake, marker = _spawn_recorder(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "bad \ud800 task", work)
    assert terminal.ok is False and terminal.error.startswith("config_write_failed:"), terminal  # type: ignore[attr-defined]
    assert not marker.exists()
    assert list(work.iterdir()) == []


def test_pre_dispatch_time_counts_against_the_attempt_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow readiness check must not extend the budget: the worker gets only what is left."""

    def slow_serve(*args: object, **kwargs: object) -> None:
        time.sleep(1.5)

    monkeypatch.setattr(executor_module, "_check_serve", slow_serve)
    fake = make_fake_opencode(tmp_path, "time.sleep(60)\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    executor = OpenCodeExecutor(opencode_bin=fake, idle_output_seconds=120.0)
    started = time.monotonic()
    terminal = run_executor(executor, "slow check", cwd, timeout=2.5)
    elapsed = time.monotonic() - started
    assert terminal.ok is False and terminal.error == "timeout", terminal  # type: ignore[attr-defined]
    # Budget 2.5 s in total: 1.5 s in the check leaves ~1.0 s for the worker.  Restarting the
    # full budget after the check would take about 4 s.
    assert elapsed < 3.4, f"pre-dispatch time was not deducted from the budget ({elapsed:.1f}s)"


def test_nothing_is_launched_when_the_pre_dispatch_checks_used_the_whole_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that answers "fine" only after the deadline must not be followed by a launch."""

    def late_but_fine(*args: object, **kwargs: object) -> None:
        time.sleep(0.6)

    monkeypatch.setattr(executor_module, "_check_serve", late_but_fine)
    launches: list[tuple[object, ...]] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> object:
        launches.append(args)
        return await real_exec(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    fake, marker = _spawn_recorder(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    terminal = run_executor(OpenCodeExecutor(opencode_bin=fake), "no budget", cwd, timeout=0.3)
    assert terminal.ok is False and terminal.error == "timeout", terminal  # type: ignore[attr-defined]
    # A process started and killed at once may not get as far as writing its marker, so the
    # launch itself is what is counted.
    assert launches == [], "a worker was launched with no time left"
    assert not marker.exists()


def test_the_worktree_context_file_and_its_git_probe_code_are_gone() -> None:
    source = Path(executor_module.__file__).read_text()
    for name in (
        "CONTEXT_FILE_NAME",
        "_write_context_file",
        "_prepare_context",
        "_context_path_is_tracked",
        "_git",
        "_probe_timeout",
        "_TrackingUnknownError",
        "_PreparationDeadlineError",
    ):
        assert not hasattr(executor_module, name), name
    assert ".verdict-context" not in source
    assert "ls-files" not in source


# ---------------------------------------------------------------------------
# PINNED_VERSION sanity
# ---------------------------------------------------------------------------


def test_pinned_version_format() -> None:
    parts = PINNED_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
