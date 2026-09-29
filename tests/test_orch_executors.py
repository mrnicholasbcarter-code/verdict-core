"""Tests for verdict.orchestration.executors (no network; fake prime binary)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from verdict.orchestration.contracts import WorkerTerminal
from verdict.orchestration.executors import (
    FaultInjectingExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
    _fault_terminal,
)
from verdict.orchestration.prime_settings import (
    one_shot_prime_settings,
    prime_retry_policy_problems,
)

ROUTE = "cc/claude-sonnet-5"


def make_fake_prime(tmp_path: Path, body: str) -> str:
    """Write an executable fake prime-agent python script; return its path."""
    script = tmp_path / "fake-prime"
    script.write_text("#!/usr/bin/env python3\nimport sys, json, os, time\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def success_payload(
    text: str = "PONG",
    model: str = ROUTE,
    provider: str = "omniroute",
    stop_reason: str = "stop",
    **extra: object,
) -> str:
    msg: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "openai-completions",
        "provider": provider,
        "model": model,
        "usage": {"input": 1, "output": 1},
        "stopReason": stop_reason,
    }
    msg.update(extra)
    doc = {"other": "keys", "messages": [{"role": "user", "content": "hi"}, msg]}
    return json.dumps(doc)


def run(executor: object, prompt: str, cwd: Path, timeout: float = 30.0) -> WorkerTerminal:
    assert isinstance(executor, (PrimeHeadlessExecutor, FaultInjectingExecutor, ScriptedExecutor))
    terminal = asyncio.run(executor.run(prompt, route_id=ROUTE, cwd=cwd, timeout_seconds=timeout))
    if isinstance(executor, PrimeHeadlessExecutor):
        assert terminal.executor_kind == "live"
    return terminal


# ------------------------------------------------------------- PrimeHeadless


def test_verdict_launch_uses_isolated_one_shot_prime_settings(tmp_path: Path) -> None:
    """The launch's EFFECTIVE settings (Prime's global<-project merge) are one-shot.

    The fake binary reproduces Prime 0.9.6 precedence: it merges the per-launch
    agent dir it is pointed at with ``<cwd>/.prime/agent/settings.json``, which
    is the file that wins in real Prime. The retry policy now travels in the
    per-launch dir, so only Verdict-owned launches are one-shot.
    """
    capture = tmp_path / "launch.json"
    prime = make_fake_prime(
        tmp_path,
        "from pathlib import Path\n"
        "import json, os, sys\n"
        "root = Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR'])\n"
        "settings = json.loads((root / 'settings.json').read_text())\n"
        "cwd = sys.argv[sys.argv.index('--cwd') + 1]\n"
        "project_file = Path(cwd) / '.prime' / 'agent' / 'settings.json'\n"
        "project = json.loads(project_file.read_text()) if project_file.exists() else {}\n"
        "effective = dict(settings)\n"
        "for key, value in project.items():\n"
        "    base = settings.get(key)\n"
        "    if isinstance(value, dict) and isinstance(base, dict):\n"
        "        effective[key] = {**base, **value}\n"
        "    else:\n"
        "        effective[key] = value\n"
        "entries = sorted(p.name for p in root.iterdir())\n"
        f"Path({str(capture)!r}).write_text(json.dumps({{'home': str(root), 'settings': effective, 'argv': sys.argv[1:], 'legacy_env': os.getenv('PRIME_AGENT_HOME'), 'entries': entries}}))\n"
        f"print({success_payload()!r})\n",
    )
    # A project file that agrees with the one-shot policy: the launch proceeds
    # and the merged result is still one-shot. (The conflicting case is refused
    # before spawn; see the test below.)
    project_agent = tmp_path / ".prime" / "agent"
    project_agent.mkdir(parents=True)
    (project_agent / "settings.json").write_text(
        json.dumps({"retry": {"enabled": False, "maxRetries": 0}})
    )
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "exact task", tmp_path)
    assert result.ok
    launch = json.loads(capture.read_text())
    assert launch["settings"]["retry"] == one_shot_prime_settings()["retry"]
    assert prime_retry_policy_problems(launch["settings"]) == []
    assert launch["settings"]["providerBackupModel"] == ""
    assert launch["home"] != str(Path.home() / ".prime" / "agent")
    assert launch["legacy_env"] is None  # mutation back to PRIME_AGENT_HOME fails above
    assert "--model" in launch["argv"]
    assert ROUTE in launch["argv"]
    # PRIME_AGENT_CODING_AGENT_DIR replaces the whole agent dir, so the launch
    # dir must expose what Prime reads from it, not just settings.json.
    assert "settings.json" in launch["entries"]
    assert not Path(launch["home"]).exists()  # per-launch state is removed


def test_verdict_launch_agent_dir_exposes_auth_and_models(tmp_path: Path) -> None:
    """A real launch resolves credentials (``auth.json``) and the provider/model
    registry (``models.json``) from the agent dir the override replaces. Dropping
    those links would leave a worker unable to authenticate, so the launch dir
    mirrors the operator's dir and the child must see both files."""
    capture = tmp_path / "launch.json"
    operator_dir = tmp_path / "operator-agent"
    (operator_dir / "skills").mkdir(parents=True)
    (operator_dir / "settings.json").write_text(json.dumps({"theme": "dark"}))
    (operator_dir / "auth.json").write_text(json.dumps({"omniroute": {"type": "api_key"}}))
    (operator_dir / "models.json").write_text(json.dumps({"providers": {"omniroute": {}}}))
    prime = make_fake_prime(
        tmp_path,
        "from pathlib import Path\n"
        "import json, os\n"
        "root = Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR'])\n"
        "seen = {p.name: json.loads(p.read_text()) for p in root.iterdir() if p.is_file()}\n"
        f"Path({str(capture)!r}).write_text(json.dumps({{'seen': seen, 'skills': (root / 'skills').is_dir()}}))\n"
        f"print({success_payload()!r})\n",
    )
    executor = PrimeHeadlessExecutor(
        prime_bin=prime, env={"PRIME_AGENT_CODING_AGENT_DIR": str(operator_dir)}
    )
    result = run(executor, "exact task", tmp_path)
    assert result.ok
    launch = json.loads(capture.read_text())
    assert launch["seen"]["auth.json"] == {"omniroute": {"type": "api_key"}}
    assert launch["seen"]["models.json"] == {"providers": {"omniroute": {}}}
    assert launch["skills"] is True
    # Operator settings survive except for the retry policy Verdict replaces.
    assert launch["seen"]["settings.json"]["theme"] == "dark"
    assert prime_retry_policy_problems(launch["seen"]["settings.json"]) == []


def test_launch_refused_when_project_settings_re_enable_prime_retry(tmp_path: Path) -> None:
    """BOD266-2: project settings win in Prime, so a conflict must fail closed.

    Prime 0.9.6 offers no per-launch escape from project precedence, so a
    project file that re-enables retry cannot be overridden at launch time. The
    executor refuses to spawn instead of running a launch that could retry
    silently, and it never reaches the binary.
    """
    spawned = tmp_path / "spawned"
    prime = make_fake_prime(
        tmp_path,
        f"from pathlib import Path\n"
        f"Path({str(spawned)!r}).write_text('spawned')\n"
        f"print({success_payload()!r})\n",
    )
    project_agent = tmp_path / ".prime" / "agent"
    project_agent.mkdir(parents=True)
    (project_agent / "settings.json").write_text(
        json.dumps({"retry": {"enabled": True, "maxRetries": 3}})
    )
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "exact task", tmp_path)
    assert not result.ok
    assert "prime retry policy not one-shot" in (result.error or "")
    assert "retry.enabled must be false" in (result.error or "")
    assert "retry.maxRetries must be 0" in (result.error or "")
    assert str(tmp_path / ".prime" / "agent" / "settings.json") in (result.error or "")
    assert not spawned.exists()  # refused before spawn: no model request at all


def test_launch_refused_when_project_settings_re_enable_usage_waits(tmp_path: Path) -> None:
    """Prime merges nested objects one level deep, so a project ``retry.provider``
    replaces the one-shot ``provider`` block wholesale and re-enables waits."""
    spawned = tmp_path / "spawned"
    prime = make_fake_prime(
        tmp_path,
        f"from pathlib import Path\n"
        f"Path({str(spawned)!r}).write_text('spawned')\n"
        f"print({success_payload()!r})\n",
    )
    project_agent = tmp_path / ".prime" / "agent"
    project_agent.mkdir(parents=True)
    (project_agent / "settings.json").write_text(
        json.dumps(
            {
                "retry": {
                    "enabled": False,
                    "maxRetries": 0,
                    "provider": {"waitForUsage": {"enabled": True, "pauseUntilReset": True}},
                }
            }
        )
    )
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "exact task", tmp_path)
    assert not result.ok
    assert "retry.provider.waitForUsage.enabled must be false" in (result.error or "")
    assert "retry.provider.waitForUsage.pauseUntilReset must be false" in (result.error or "")
    assert not spawned.exists()


def test_launch_refused_when_project_settings_set_a_backup_model(tmp_path: Path) -> None:
    """A backup model silently reroutes a quota-blocked turn to another model,
    which would break Verdict's one-route-per-attempt attribution."""
    spawned = tmp_path / "spawned"
    prime = make_fake_prime(
        tmp_path,
        f"from pathlib import Path\n"
        f"Path({str(spawned)!r}).write_text('spawned')\n"
        f"print({success_payload()!r})\n",
    )
    project_agent = tmp_path / ".prime" / "agent"
    project_agent.mkdir(parents=True)
    (project_agent / "settings.json").write_text(
        json.dumps({"retry": {"enabled": False, "maxRetries": 0}, "providerBackupModel": "x/y"})
    )
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "exact task", tmp_path)
    assert not result.ok
    assert "providerBackupModel must be empty" in (result.error or "")
    assert not spawned.exists()


def test_success_parses_last_assistant_text(tmp_path: Path) -> None:
    payload = success_payload("PONG")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "ping", tmp_path)
    assert result.ok
    assert result.output == "PONG"
    assert result.model == ROUTE
    assert result.stop_reason == "stop"
    assert result.duration_seconds > 0


def test_success_end_turn_and_multiple_text_blocks(tmp_path: Path) -> None:
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "A"},
            {"type": "tool_use", "id": "x"},
            {"type": "text", "text": "B"},
        ],
        "provider": "omniroute",
        "model": ROUTE,
        "stopReason": "end_turn",
    }
    payload = json.dumps({"messages": [msg]})
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert result.ok
    assert result.output == "AB"


def test_json_lines_takes_last_assistant(tmp_path: Path) -> None:
    line1 = json.dumps(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "old"}],
            "provider": "omniroute",
            "model": ROUTE,
            "stopReason": "stop",
        }
    )
    line2 = json.dumps(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "new"}],
            "provider": "omniroute",
            "model": ROUTE,
            "stopReason": "stop",
        }
    )
    prime = make_fake_prime(tmp_path, f"print({line1!r})\nprint({line2!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert result.ok
    assert result.output == "new"


def test_model_mismatch(tmp_path: Path) -> None:
    payload = success_payload(model="cc/other-model")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.error == "model_mismatch"
    assert result.model == "cc/other-model"


def test_provider_mismatch(tmp_path: Path) -> None:
    payload = success_payload(provider="anthropic")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.error == "model_mismatch"


def test_error_message_with_status_code(tmp_path: Path) -> None:
    payload = success_payload(errorMessage="429 rate limit exceeded, retry later")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.status_code == 429
    assert "rate limit" in result.error


def test_error_message_401(tmp_path: Path) -> None:
    payload = success_payload(errorMessage="authentication failed (401)")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.status_code == 401


def test_bad_stop_reason(tmp_path: Path) -> None:
    payload = success_payload(stop_reason="max_tokens")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.error == "no_final_answer"


def test_empty_text(tmp_path: Path) -> None:
    payload = success_payload(text="")
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.error == "empty_output"


def test_missing_assistant_message(tmp_path: Path) -> None:
    payload = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
    prime = make_fake_prime(tmp_path, f"print({payload!r})\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.error == "no_final_answer"


def test_non_json_stdout(tmp_path: Path) -> None:
    prime = make_fake_prime(tmp_path, "print('this is not json at all')\n")
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert result.error.startswith("malformed:")


def test_nonzero_exit_uses_stderr_tail(tmp_path: Path) -> None:
    body = "sys.stderr.write('boom: ' + 'x' * 1000)\nsys.exit(3)\n"
    prime = make_fake_prime(tmp_path, body)
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path)
    assert not result.ok
    assert len(result.error) <= 400
    assert result.error.endswith("x")


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.pid"
    body = (
        "import subprocess\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    prime = make_fake_prime(tmp_path, body)
    started = time.monotonic()
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "go", tmp_path, timeout=1.0)
    elapsed = time.monotonic() - started
    assert not result.ok
    assert result.error == "timeout"
    assert elapsed < 10.0
    # the grandchild (same process group) must be dead too
    grandchild_pid = int(marker.read_text())
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(grandchild_pid, signal.SIGKILL)
        pytest.fail("grandchild survived the process-group kill")


def test_prompt_starting_with_dash_is_safe(tmp_path: Path) -> None:
    # fake prime echoes back the last argv element as the assistant text
    body = (
        "text = sys.argv[-1]\n"
        "msg = {'role': 'assistant', 'content': [{'type': 'text', 'text': text}],\n"
        f"       'provider': 'omniroute', 'model': {ROUTE!r}, 'stopReason': 'stop'}}\n"
        "print(json.dumps({'messages': [msg]}))\n"
    )
    prime = make_fake_prime(tmp_path, body)
    result = run(PrimeHeadlessExecutor(prime_bin=prime), "--help me", tmp_path)
    assert result.ok
    assert result.output == "--help me"


def test_missing_binary_is_terminal_not_raise(tmp_path: Path) -> None:
    result = run(PrimeHeadlessExecutor(prime_bin=str(tmp_path / "nope")), "go", tmp_path)
    assert not result.ok
    assert "spawn failed" in result.error


def test_command_flags_and_thinking(tmp_path: Path) -> None:
    body = (
        "args = sys.argv[1:]\n"
        "msg = {'role': 'assistant', 'content': [{'type': 'text', 'text': ' '.join(args)}],\n"
        f"       'provider': 'omniroute', 'model': {ROUTE!r}, 'stopReason': 'stop'}}\n"
        "print(json.dumps({'messages': [msg]}))\n"
    )
    prime = make_fake_prime(tmp_path, body)
    executor = PrimeHeadlessExecutor(prime_bin=prime, thinking="high")
    result = run(executor, "PROMPT", tmp_path)
    assert result.ok
    argv = result.output.split(" ")
    assert argv[0] == "-p"
    assert "--no-session" in argv
    assert "--provider" in argv and "omniroute" in argv
    assert "--model" in argv and ROUTE in argv
    assert "--thinking" in argv and "high" in argv
    for flag in ("-nc", "-ns", "-ne"):
        assert flag in argv
    assert argv[-2:] == ["--", "PROMPT"]


# ----------------------------------------------------------- FaultInjecting


FAULT_EXPECTATIONS: list[tuple[str, bool, int | None, str]] = [
    ("quota", False, 429, "You have exceeded your usage limit; resets in 2h"),
    ("rate_limit", False, 429, "rate limited"),
    ("auth", False, 401, "authentication failed"),
    ("payment", False, 402, "payment required"),
    ("forbidden", False, 403, "forbidden"),
    ("server", False, 503, "upstream server error"),
    ("timeout", False, None, "timeout"),
    ("transport", False, None, "transport: connection reset"),
    ("no_final", False, None, "no_final_answer"),
    ("malformed", False, None, "malformed: not json"),
    ("mismatch", False, None, "model_mismatch"),
]


def _ok_script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
    return WorkerTerminal(ok=True, output="real", model=route_id)


@pytest.mark.parametrize(("kind", "ok", "status", "error"), FAULT_EXPECTATIONS)
def test_fault_kinds(kind: str, ok: bool, status: int | None, error: str, tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: [kind]})
    result = run(executor, "go", tmp_path)
    assert result.ok is ok
    assert result.status_code == status
    assert result.error == error
    assert result.model == ROUTE
    assert result.session_ref == f"fault-injected:{kind}"
    assert result.executor_kind == "fault-injected"


def test_fault_rate_limit_retry_after(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: ["rate_limit"]})
    result = run(executor, "go", tmp_path)
    assert result.retry_after_seconds == 30


def test_fault_empty_is_ok_with_empty_output(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: ["empty"]})
    result = run(executor, "go", tmp_path)
    assert result.ok
    assert result.output == ""
    assert result.session_ref == "fault-injected:empty"
    assert result.executor_kind == "fault-injected"


def test_fault_hang_sleeps_then_times_out(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: ["hang"]})
    started = time.monotonic()
    result = run(executor, "go", tmp_path, timeout=0.2)
    assert time.monotonic() - started >= 0.2
    assert not result.ok
    assert result.error == "timeout"
    assert result.session_ref == "fault-injected:hang"
    assert result.executor_kind == "fault-injected"


def test_fault_queue_drains_then_delegates(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: ["server"]})
    first = run(executor, "go", tmp_path)
    second = run(executor, "go", tmp_path)
    assert not first.ok and first.status_code == 503
    assert second.ok and second.output == "real"
    assert first.executor_kind == "fault-injected"
    assert second.executor_kind == "scripted"


def test_fault_wildcard_key(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {"*": ["auth"]})
    result = run(executor, "go", tmp_path)
    assert not result.ok
    assert result.status_code == 401
    assert result.session_ref == "fault-injected:auth"


def test_fault_route_key_takes_priority_over_wildcard(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(
        ScriptedExecutor(_ok_script), {ROUTE: ["quota"], "*": ["auth"]}
    )
    result = run(executor, "go", tmp_path)
    assert result.status_code == 429


# --------------------------------------------------------------- Scripted


def test_scripted_sync(tmp_path: Path) -> None:
    def script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        return WorkerTerminal(ok=True, output=f"{prompt}|{route_id}|{cwd.name}")

    result = run(ScriptedExecutor(script), "hello", tmp_path)
    assert result.ok
    assert result.output == f"hello|{ROUTE}|{tmp_path.name}"
    assert result.executor_kind == "scripted"


def test_scripted_async(tmp_path: Path) -> None:
    async def script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        await asyncio.sleep(0)
        return WorkerTerminal(ok=False, error="scripted failure", model=route_id)

    result = run(ScriptedExecutor(script), "hello", tmp_path)
    assert not result.ok
    assert result.error == "scripted failure"
    assert result.executor_kind == "scripted"


async def test_fault_keys_match_provider_prefix_and_node(tmp_path: Path) -> None:
    from verdict.orchestration.executors import FaultInjectingExecutor, ScriptedExecutor

    inner = ScriptedExecutor(
        lambda p, r, c: WorkerTerminal(ok=True, output="RESULT: DONE", model=r)
    )
    fx = FaultInjectingExecutor(inner, {"cc/*": ["quota"], "@wrap": ["auth"]})
    first = await fx.run(
        "x", route_id="cc/claude-sonnet-5", cwd=tmp_path / "slugify-a1", timeout_seconds=5
    )
    assert first.status_code == 429 and first.session_ref == "fault-injected:quota"
    second = await fx.run(
        "x", route_id="cc/claude-sonnet-5", cwd=tmp_path / "slugify-a2", timeout_seconds=5
    )
    assert second.ok
    third = await fx.run("x", route_id="cx/gpt-5.5", cwd=tmp_path / "wrap-a1", timeout_seconds=5)
    assert third.status_code == 401


async def test_fault_key_by_dispatch_ordinal(tmp_path: Path) -> None:
    from verdict.orchestration.executors import FaultInjectingExecutor, ScriptedExecutor

    inner = ScriptedExecutor(
        lambda p, r, c: WorkerTerminal(ok=True, output="RESULT: DONE", model=r)
    )
    fx = FaultInjectingExecutor(inner, {"#2": ["quota"]})
    assert (await fx.run("x", route_id="cc/a", cwd=tmp_path / "n1-a1", timeout_seconds=5)).ok
    second = await fx.run("x", route_id="cc/b", cwd=tmp_path / "n2-a1", timeout_seconds=5)
    assert second.status_code == 429
    assert (await fx.run("x", route_id="cc/b", cwd=tmp_path / "n2-a2", timeout_seconds=5)).ok


async def test_fault_worker_key_skips_planner_calls(tmp_path: Path) -> None:
    """worker#1 is the first attempt worktree even when planning used two calls."""
    inner = ScriptedExecutor(
        lambda p, r, c: WorkerTerminal(ok=True, output="RESULT: DONE", model=r)
    )
    fx = FaultInjectingExecutor(inner, {"worker#1": ["rate_limit"]})
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = await fx.run("plan", route_id="cc/planner", cwd=repo, timeout_seconds=5)
    repair = await fx.run("repair", route_id="cc/planner", cwd=repo, timeout_seconds=5)
    assert plan.ok and repair.ok
    first_worker = await fx.run(
        "impl", route_id="cc/worker", cwd=tmp_path / "parser-a1", timeout_seconds=5
    )
    assert first_worker.status_code == 429
    assert first_worker.session_ref == "fault-injected:rate_limit"
    second_worker = await fx.run(
        "impl", route_id="cc/worker", cwd=tmp_path / "cli-a1", timeout_seconds=5
    )
    assert second_worker.ok


async def test_fault_hash_n_still_counts_planner(tmp_path: Path) -> None:
    """#N still counts planner calls; #2 is plan-repair when planning used two calls."""
    inner = ScriptedExecutor(
        lambda p, r, c: WorkerTerminal(ok=True, output="RESULT: DONE", model=r)
    )
    fx = FaultInjectingExecutor(inner, {"#2": ["rate_limit"]})
    repo = tmp_path / "repo"
    repo.mkdir()
    assert (await fx.run("plan", route_id="cc/planner", cwd=repo, timeout_seconds=5)).ok
    repair = await fx.run("repair", route_id="cc/planner", cwd=repo, timeout_seconds=5)
    assert repair.status_code == 429
    worker = await fx.run(
        "impl", route_id="cc/worker", cwd=tmp_path / "parser-a1", timeout_seconds=5
    )
    assert worker.ok


def test_installed_prime_096_declares_config_directory_override() -> None:
    """Pin launch env to Prime 0.9.6's published real config contract."""
    import shutil

    binary = shutil.which("prime-agent")
    if binary is None:
        pytest.skip("prime-agent not installed")
    package = Path(binary).resolve().parent
    readme = (package / "README.md").read_text()
    usage = (package / "docs" / "usage.md").read_text()
    assert "PRIME_AGENT_CODING_AGENT_DIR" in readme
    assert "Override config directory" in readme
    assert "PRIME_AGENT_CODING_AGENT_DIR" in usage
    assert "default is `~/.prime/agent`" in usage


@pytest.mark.parametrize("kind", ["empty", "rate_limit", "server"])
def test_fault_provenance_does_not_depend_on_session_ref(kind: str) -> None:
    terminal = _fault_terminal(kind, ROUTE, "renamed-session")
    assert terminal.executor_kind == "fault-injected"
    assert terminal.session_ref == "renamed-session"


@pytest.mark.parametrize("kind", ["live", "scripted", ""])
def test_fault_delegation_preserves_terminal_provenance(tmp_path: Path, kind: str) -> None:
    terminal = WorkerTerminal(
        ok=True, output="done", model=ROUTE, session_ref="fault-injected:opaque", executor_kind=kind
    )

    class InnerExecutor:
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            return terminal

    result = run(FaultInjectingExecutor(InnerExecutor(), {}), "go", tmp_path)
    assert result is terminal
    assert result.executor_kind == kind


def test_scripted_executor_stamps_kind_without_mutating_script_result(tmp_path: Path) -> None:
    terminal = WorkerTerminal(
        ok=True, output="fixture", model=ROUTE, session_ref="script-session", executor_kind="live"
    )
    result = run(ScriptedExecutor(lambda p, r, c: terminal), "go", tmp_path)
    assert result == replace(terminal, executor_kind="scripted")
    assert terminal.executor_kind == "live"
