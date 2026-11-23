"""Tests for OpenCodeExecutor and helpers (no live model calls; fake binary).

All subprocess calls go to a tiny Python script written to tmp_path.  The
real opencode binary is never invoked.  Config env vars are overridden so the
real ~/.opencode or ~/.config/opencode are never read or written.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from verdict.orchestration.executor_opencode import (
    CONTEXT_FILE_NAME,
    PINNED_VERSION,
    OpenCodeExecutor,
    _attempt_session_id,
    _build_child_env,
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
    if capture.exists():
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
    executor = OpenCodeExecutor(opencode_bin=fake, env={"HOME": str(tmp_path), "PATH": ""})
    run_executor(executor, "cleanup test", cwd)
    if capture.exists():
        dirs = json.loads(capture.read_text())
        for key, path in dirs.items():
            parent = Path(path).parent if path else None
            if parent and "verdict-oc-" in str(parent):
                assert not Path(path).parent.exists(), (
                    f"Temp dir {path!r} ({key}) was not cleaned up"
                )


# ---------------------------------------------------------------------------
# PINNED_VERSION sanity
# ---------------------------------------------------------------------------


def test_pinned_version_format() -> None:
    parts = PINNED_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# OpenCodeUnitExecutor — Task 5.1 (autodev model-tier rung)
# ---------------------------------------------------------------------------


def test_opencode_unit_executor_applied_on_exit_zero(tmp_path: Path) -> None:
    """Exit 0 → PatchAttempt.outcome == 'applied', usage populated."""
    from verdict.autodev_run import OpenCodeUnitExecutor
    from verdict.patch_executor import PatchExecutorConfig
    from verdict.work_unit import WorkUnit

    payload = ndjson_success("feature implemented")
    fake = make_fake_opencode(
        tmp_path,
        f"import sys\nsys.stdout.write({payload!r})\nsys.stdout.write('\\n')\nsys.exit(0)\n",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = PatchExecutorConfig(model=ROUTE, timeout_seconds=10.0)
    oc = OpenCodeExecutor(opencode_bin=fake)
    unit_exec = OpenCodeUnitExecutor(repo, cfg, oc)

    unit = WorkUnit(
        unit_id="u1",
        objective="implement the foo feature",
        owned_files=("src/foo.py",),
        verification_command=("true",),
    )
    attempt = unit_exec.execute_unit(unit)
    assert attempt.applied is True
    assert attempt.unit_id == "u1"
    assert attempt.model == ROUTE
    assert attempt.usage.reported is True
    assert attempt.usage.prompt_tokens == 100
    assert attempt.usage.completion_tokens == 42


def test_opencode_unit_executor_error_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit → outcome == 'error', applied is False."""
    from verdict.autodev_run import OpenCodeUnitExecutor
    from verdict.patch_executor import PatchExecutorConfig
    from verdict.work_unit import WorkUnit

    fake = make_fake_opencode(tmp_path, "import sys\nsys.exit(1)\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = PatchExecutorConfig(model=ROUTE, timeout_seconds=10.0)
    unit_exec = OpenCodeUnitExecutor(repo, cfg, OpenCodeExecutor(opencode_bin=fake))

    unit = WorkUnit(
        unit_id="u2",
        objective="fix the bug",
        owned_files=("src/foo.py",),
        verification_command=("true",),
    )
    attempt = unit_exec.execute_unit(unit)
    assert attempt.applied is False
    assert attempt.unit_id == "u2"


def test_opencode_unit_executor_has_patch_executor_compatible_interface(tmp_path: Path) -> None:
    """OpenCodeUnitExecutor satisfies the PatchExecutor-like interface."""
    from verdict.autodev_run import OpenCodeUnitExecutor
    from verdict.patch_executor import PatchExecutorConfig

    fake = make_fake_opencode(tmp_path, "import sys\nsys.exit(0)\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = PatchExecutorConfig(model=ROUTE)
    unit_exec = OpenCodeUnitExecutor(repo, cfg, OpenCodeExecutor(opencode_bin=fake))

    assert isinstance(unit_exec.config, PatchExecutorConfig)
    assert unit_exec.config.model == ROUTE
    assert isinstance(unit_exec.response_headers, dict)
    assert len(unit_exec.response_headers) == 0
    assert callable(unit_exec.execute_unit)


def test_opencode_unit_executor_includes_context_in_prompt(tmp_path: Path) -> None:
    """Unit context is appended to the objective in the prompt."""
    from verdict.autodev_run import OpenCodeUnitExecutor
    from verdict.patch_executor import PatchExecutorConfig
    from verdict.work_unit import WorkUnit

    capture = tmp_path / "argv.json"
    fake = make_fake_opencode(
        tmp_path,
        f"import sys, json\nimport pathlib\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.exit(0)\n",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = PatchExecutorConfig(model=ROUTE, timeout_seconds=10.0)
    unit_exec = OpenCodeUnitExecutor(repo, cfg, OpenCodeExecutor(opencode_bin=fake))

    unit = WorkUnit(
        unit_id="u3",
        objective="add docstring",
        owned_files=("src/foo.py",),
        verification_command=("true",),
        context="# relevant code here",
    )
    unit_exec.execute_unit(unit)
    argv = json.loads(capture.read_text())
    # The last argv is the prompt passed after `--`
    prompt = argv[-1]
    assert "add docstring" in prompt
    assert "relevant code here" in prompt
