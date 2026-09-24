"""Tests for verdict.orchestration.executors (no network; fake prime binary)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import time
from pathlib import Path

import pytest

from verdict.orchestration.contracts import WorkerTerminal
from verdict.orchestration.executors import (
    FaultInjectingExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
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
    return asyncio.run(executor.run(prompt, route_id=ROUTE, cwd=cwd, timeout_seconds=timeout))


# ------------------------------------------------------------- PrimeHeadless


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


def test_fault_hang_sleeps_then_times_out(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: ["hang"]})
    started = time.monotonic()
    result = run(executor, "go", tmp_path, timeout=0.2)
    assert time.monotonic() - started >= 0.2
    assert not result.ok
    assert result.error == "timeout"
    assert result.session_ref == "fault-injected:hang"


def test_fault_queue_drains_then_delegates(tmp_path: Path) -> None:
    executor = FaultInjectingExecutor(ScriptedExecutor(_ok_script), {ROUTE: ["server"]})
    first = run(executor, "go", tmp_path)
    second = run(executor, "go", tmp_path)
    assert not first.ok and first.status_code == 503
    assert second.ok and second.output == "real"


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


def test_scripted_async(tmp_path: Path) -> None:
    async def script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        await asyncio.sleep(0)
        return WorkerTerminal(ok=False, error="scripted failure", model=route_id)

    result = run(ScriptedExecutor(script), "hello", tmp_path)
    assert not result.ok
    assert result.error == "scripted failure"


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
