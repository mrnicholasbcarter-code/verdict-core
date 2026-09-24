"""WorkerExecutor adapters: Prime headless harness, fault injection, scripting.

``PrimeHeadlessExecutor`` runs one prompt on one exact OmniRoute route via the
``prime-agent`` headless CLI and converts every outcome — success, provider
error, timeout, malformed output — into a :class:`WorkerTerminal`. It never
raises for child failures.

``FaultInjectingExecutor`` wraps any executor and pops queued synthetic
failures per route for chaos proofs. ``ScriptedExecutor`` delegates to a
callable for deterministic tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import WorkerExecutor, WorkerTerminal

__all__ = ["FaultInjectingExecutor", "PrimeHeadlessExecutor", "ScriptedExecutor"]

_STATUS_RE = re.compile(r"\b([45]\d{2})\b")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_STDERR_TAIL_CHARS = 400
_TERM_GRACE_SECONDS = 5.0


def _sanitize(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    clean = _CONTROL_RE.sub("", text).strip()
    return clean[-limit:]


def _parse_status_code(message: str) -> int | None:
    match = _STATUS_RE.search(message)
    return int(match.group(1)) if match else None


def _iter_json_values(stdout: str) -> tuple[list[Any], bool]:
    """Parse stdout as one JSON document or JSON-lines.

    Returns ``(values, parsed_any)``.
    """
    text = stdout.strip()
    if not text:
        return [], False
    try:
        return [json.loads(text)], True
    except ValueError:
        pass
    values: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(json.loads(line))
        except ValueError:
            continue
    return values, bool(values)


def _collect_messages(values: list[Any]) -> list[dict[str, Any]]:
    """Find message dicts in parsed JSON values (tolerant of extra keys)."""
    messages: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, dict):
            inner = value.get("messages")
            if isinstance(inner, list):
                messages.extend(m for m in inner if isinstance(m, dict))
            elif "role" in value:
                messages.append(value)
        elif isinstance(value, list):
            messages.extend(m for m in value if isinstance(m, dict) and "role" in m)
    return messages


def _text_of(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


class PrimeHeadlessExecutor:
    """Run one prompt on one exact route through the Prime headless CLI."""

    def __init__(
        self,
        prime_bin: str = "prime-agent",
        provider: str = "omniroute",
        extra_args: tuple[str, ...] = ("-nc", "-ns", "-ne"),
        thinking: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.prime_bin = prime_bin
        self.provider = provider
        self.extra_args = tuple(extra_args)
        self.thinking = thinking
        self.env = dict(env) if env is not None else None

    def _command(self, prompt: str, route_id: str, cwd: Path) -> list[str]:
        cmd = [
            self.prime_bin,
            "-p",
            "--no-session",
            "--mode",
            "json",
            "--provider",
            self.provider,
            "--model",
            route_id,
            "--cwd",
            str(cwd),
            *self.extra_args,
        ]
        if self.thinking is not None:
            cmd.extend(["--thinking", self.thinking])
        cmd.append("--")  # prompt is data, never a flag
        cmd.append(prompt)
        return cmd

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        started = time.monotonic()
        child_env = {**os.environ, **self.env} if self.env is not None else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command(prompt, route_id, cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=child_env,
                start_new_session=True,
            )
        except OSError as exc:
            return WorkerTerminal(
                ok=False,
                model=route_id,
                error=f"spawn failed: {_sanitize(str(exc))}",
                duration_seconds=time.monotonic() - started,
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await self._kill_group(proc)
            return WorkerTerminal(
                ok=False,
                model=route_id,
                error="timeout",
                duration_seconds=time.monotonic() - started,
            )
        duration = time.monotonic() - started
        return self._interpret(
            stdout=stdout_b.decode("utf-8", "replace"),
            stderr=stderr_b.decode("utf-8", "replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
            route_id=route_id,
            duration=duration,
        )

    async def _kill_group(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the whole process group; escalate to SIGKILL after a grace period."""
        pgid: int | None
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = None
        self._signal_group(proc, pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
        except asyncio.TimeoutError:
            self._signal_group(proc, pgid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)

    @staticmethod
    def _signal_group(
        proc: asyncio.subprocess.Process, pgid: int | None, sig: signal.Signals
    ) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    def _interpret(
        self, *, stdout: str, stderr: str, returncode: int, route_id: str, duration: float
    ) -> WorkerTerminal:
        values, parsed_any = _iter_json_values(stdout)
        messages = _collect_messages(values)
        assistant: dict[str, Any] | None = None
        for message in messages:
            if message.get("role") == "assistant":
                assistant = message
        if assistant is None:
            if returncode != 0:
                tail = _sanitize(stderr) or f"exit {returncode} with no assistant message"
                return WorkerTerminal(
                    ok=False, model=route_id, error=tail, duration_seconds=duration
                )
            if not parsed_any:
                return WorkerTerminal(
                    ok=False,
                    model=route_id,
                    error=f"malformed: {_sanitize(stdout, 200) or 'empty stdout'}",
                    duration_seconds=duration,
                )
            return WorkerTerminal(
                ok=False, model=route_id, error="no_final_answer", duration_seconds=duration
            )

        reported_model = str(assistant.get("model", ""))
        reported_provider = str(assistant.get("provider", ""))
        stop_reason = str(assistant.get("stopReason", ""))
        text = _text_of(assistant)

        if reported_provider != self.provider or reported_model != route_id:
            return WorkerTerminal(
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="model_mismatch",
                duration_seconds=duration,
            )
        error_message = assistant.get("errorMessage")
        if error_message:
            error_text = _sanitize(str(error_message))
            return WorkerTerminal(
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error=error_text,
                status_code=_parse_status_code(error_text),
                duration_seconds=duration,
            )
        if stop_reason not in {"stop", "end_turn"}:
            return WorkerTerminal(
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="no_final_answer",
                duration_seconds=duration,
            )
        if not text.strip():
            return WorkerTerminal(
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="empty_output",
                duration_seconds=duration,
            )
        return WorkerTerminal(
            ok=True,
            output=text,
            model=reported_model,
            stop_reason=stop_reason,
            duration_seconds=duration,
        )


def _fault_terminal(kind: str, route_id: str, session_ref: str) -> WorkerTerminal:
    codes = {
        "quota": 429,
        "route_quota": 429,
        "rate_limit": 429,
        "auth": 401,
        "payment": 402,
        "forbidden": 403,
        "server": 503,
    }
    errors = {
        "quota": "You have exceeded your usage limit; resets in 2h",
        # Model-scoped usage cap (e.g. a per-model weekly limit): the provider's
        # other models stay usable, so only the route cools down.
        "route_quota": "model usage limit reached for this model; resets in 2h",
        "rate_limit": "rate limited",
        "auth": "authentication failed",
        "payment": "payment required",
        "forbidden": "forbidden",
        "server": "upstream server error",
        "timeout": "timeout",
        "hang": "timeout",
        "transport": "transport: connection reset",
        "no_final": "no_final_answer",
        "malformed": "malformed: not json",
        "mismatch": "model_mismatch",
    }
    if kind == "empty":
        return WorkerTerminal(ok=True, output="", model=route_id, session_ref=session_ref)
    if kind not in errors:
        raise ValueError(f"unknown fault kind {kind!r}")
    return WorkerTerminal(
        ok=False,
        model=route_id,
        session_ref=session_ref,
        error=errors[kind],
        status_code=codes.get(kind),
        retry_after_seconds=30 if kind == "rate_limit" else None,
    )


class FaultInjectingExecutor:
    """Pop queued synthetic faults per route before delegating to ``inner``."""

    def __init__(self, inner: WorkerExecutor, faults: Mapping[str, list[str]]) -> None:
        self.inner = inner
        self._faults: dict[str, list[str]] = {k: list(v) for k, v in faults.items()}
        self._dispatches = 0  # worker dispatches seen (planning included), for "#N" keys

    def _keys(self, route_id: str, cwd: Path) -> list[str]:
        """Match order: exact route, provider prefix ("cc/*"), node ("@slugify"), "*".

        The node id is taken from the attempt worktree name ``<node>-a<N>`` so a
        chaos run can target "the first attempt of node X, whatever route the
        eligibility ladder picked" without predicting ranking.
        """
        provider = route_id.split("/", 1)[0] + "/*"
        node = cwd.name.rsplit("-a", 1)[0] if "-a" in cwd.name else cwd.name
        # "#N": the N-th executor call of the run, whatever node/route it is. Lets a
        # chaos run hit a worker without predicting planner-chosen node ids.
        return [f"#{self._dispatches}", route_id, provider, "@" + node, "*"]

    def _pop_fault(self, route_id: str, cwd: Path | None = None) -> str | None:
        for key in self._keys(route_id, cwd or Path(".")):
            queue = self._faults.get(key)
            if queue:
                return queue.pop(0)
        return None

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self._dispatches += 1
        kind = self._pop_fault(route_id, cwd)
        if kind is None:
            return await self.inner.run(
                prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
            )
        if kind == "hang":
            await asyncio.sleep(timeout_seconds)
        return _fault_terminal(kind, route_id, f"fault-injected:{kind}")


ScriptFn = Callable[[str, str, Path], "WorkerTerminal | Awaitable[WorkerTerminal]"]


class ScriptedExecutor:
    """Deterministic executor backed by a plain (sync or async) callable."""

    def __init__(self, script: ScriptFn) -> None:
        self.script = script

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        result = self.script(prompt, route_id, cwd)
        if isinstance(result, WorkerTerminal):
            return result
        return await result
