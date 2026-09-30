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
import math
import os
import posixpath
import re
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import httpx

from verdict.orchestration.contracts import AttemptUsage, WorkerExecutor, WorkerTerminal
from verdict.orchestration.prime_settings import (
    PRIME_AGENT_DIR_ENV,
    PROJECT_SETTINGS_RELPATH,
    default_prime_agent_dir,
    effective_prime_settings,
    prepare_launch_agent_dir,
    prime_retry_policy_problems,
)

__all__ = [
    "DirectGatewayExecutor",
    "FaultInjectingExecutor",
    "MixedExecutor",
    "PrimeHeadlessExecutor",
    "ScriptedExecutor",
]

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


_STREAMING_TYPES = frozenset({"message_start", "message_update"})


def _collect_messages(values: list[Any]) -> list[dict[str, Any]]:
    """Collect message dicts from parsed Prime JSON-lines output.

    Only collects from **final** events:

    * ``agent_end`` → ``"messages"`` (plural list)
    * ``message_end`` / ``turn_end`` → ``"message"`` (singular dict)
    * Top-level dicts with ``"role"`` (no ``type`` field) and bare lists

    ``message_start`` and ``message_update`` are **ignored** — they carry
    streaming partials that must not compete with final copies.

    Deduplication identity:

    * ``responseId`` when present on the message dict
    * ``(timestamp, role)`` when both are present but ``responseId`` is not
    * Otherwise no dedup — the message is treated as distinct

    Merge rule: fields from the latest final copy win, but if that copy
    lacks ``usage`` and an earlier copy has it the earlier usage is kept.
    First-appearance order by identity is preserved.
    """

    raw: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, dict):
            evt_type = value.get("type")
            if evt_type in _STREAMING_TYPES:
                continue  # skip partials
            inner = value.get("messages")
            if isinstance(inner, list):
                raw.extend(m for m in inner if isinstance(m, dict))
            else:
                msg = value.get("message")
                if isinstance(msg, dict) and "role" in msg:
                    raw.append(msg)
                elif "role" in value and evt_type is None:
                    # bare message dict (no Prime event wrapper)
                    raw.append(value)
        elif isinstance(value, list):
            raw.extend(m for m in value if isinstance(m, dict) and "role" in m)

    # --- identity-based merge ------------------------------------------------
    # Order: dict keyed by identity → list of copies in appearance order.
    # Messages with no usable identity get a unique sentinel so they are never
    # merged with anything else.
    _no_id_counter = 0
    order: dict[Any, list[dict[str, Any]]] = {}
    appearance: list[Any] = []  # first-seen identity order
    for msg in raw:
        rid = msg.get("responseId")
        if rid is not None:
            key: Any = ("rid", rid)
        else:
            ts = msg.get("timestamp")
            role = msg.get("role")
            if ts is not None and role is not None:
                key = ("ts", ts, role)
            else:
                key = ("_no_id", _no_id_counter)
                _no_id_counter += 1
        if key not in order:
            order[key] = []
            appearance.append(key)
        order[key].append(msg)

    # Merge each group: last copy wins fields; preserve earlier usage if the
    # last copy lacks it.
    merged: list[dict[str, Any]] = []
    for key in appearance:
        copies = order[key]
        final = dict(copies[-1])  # shallow copy of latest
        if "usage" not in final or final.get("usage") is None:
            for earlier in reversed(copies[:-1]):
                if "usage" in earlier and earlier["usage"] is not None:
                    final["usage"] = earlier["usage"]
                    break
        merged.append(final)
    return merged


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


def _as_int(value: Any) -> int | None:
    """Best-effort int for a reported token count; never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> float | None:
    """Best-effort float for a reported cost; never raises, rejects NaN/inf."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


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
        child_env = {**os.environ, **self.env} if self.env is not None else dict(os.environ)
        # Prime's retry policy is semantic: each Verdict launch must make at
        # most one model request before Verdict classifies its terminal. Only
        # Verdict-owned launches are one-shot, so the policy travels in a
        # per-launch agent dir, not in the tracked project file that every
        # session in this checkout would otherwise inherit.
        launch_config = tempfile.TemporaryDirectory(prefix="verdict-prime-")
        # PRIME_AGENT_CODING_AGENT_DIR replaces the WHOLE agent dir, so the
        # launch dir mirrors auth.json, models.json, skills and the rest.
        # Without that, a real launch would lose its credentials and registry.
        launch_dir = prepare_launch_agent_dir(
            Path(launch_config.name), source=default_prime_agent_dir(child_env)
        )
        config_dir = launch_dir.path
        child_env[PRIME_AGENT_DIR_ENV] = str(config_dir)
        # Project settings win over this per-launch config dir in Prime, and
        # Prime 0.9.6 has no per-launch escape, so assert the EFFECTIVE merge.
        policy_problems = prime_retry_policy_problems(
            effective_prime_settings(cwd=cwd, config_dir=config_dir)
        )
        if policy_problems:
            launch_config.cleanup()
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error=(
                    "prime retry policy not one-shot: "
                    + "; ".join(policy_problems)
                    + f" (fix {cwd / PROJECT_SETTINGS_RELPATH})"
                ),
                duration_seconds=time.monotonic() - started,
            )
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
            launch_config.cleanup()
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error=f"spawn failed: {_sanitize(str(exc))}",
                duration_seconds=time.monotonic() - started,
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await self._kill_group(proc)
            launch_config.cleanup()
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error="timeout",
                duration_seconds=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            # BOD-276: cancel_node/cancel_run cancels this task; kill the
            # subprocess (and its process group) before propagating so a
            # cancelled run leaves no paid model calls running.
            with contextlib.suppress(BaseException):
                await self._kill_group(proc)
            with contextlib.suppress(Exception):
                launch_config.cleanup()
            raise
        duration = time.monotonic() - started
        launch_config.cleanup()
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

    @staticmethod
    def _extract_usage_single(raw: dict[str, Any]) -> tuple[int | None, int | None, float | None]:
        """Parse one usage dict into (input_tokens, output_tokens, cost_usd)."""
        # Accept both input/output (Prime stdout) and prompt_tokens/completion_tokens (OpenAI)
        inp = raw.get("input") if raw.get("input") is not None else raw.get("prompt_tokens")
        out = raw.get("output") if raw.get("output") is not None else raw.get("completion_tokens")
        input_tokens = _as_int(inp)
        output_tokens = _as_int(out)
        # cost_usd only if explicitly present — never fabricated.
        # Prime reports ``cost`` as a breakdown dict
        # ({"input", "output", "cacheRead", "cacheWrite", "total"}); use its total.
        cost_raw = raw.get("cost_usd") if raw.get("cost_usd") is not None else raw.get("cost")
        if isinstance(cost_raw, dict):
            cost_raw = cost_raw.get("total")
        cost_usd = _as_float(cost_raw)
        return input_tokens, output_tokens, cost_usd

    @staticmethod
    def _extract_usage(
        messages_or_single: list[dict[str, Any]] | dict[str, Any] | None,
    ) -> AttemptUsage | None:
        """Sum token usage over all distinct assistant messages of the attempt.

        Accepts either a list of collected messages (the normal path from
        ``_interpret`` after ``_collect_messages``) **or** a single message
        dict / ``None`` for backward compatibility.

        Each assistant turn re-sends the full conversation history, so
        ``input_tokens`` per turn is independently billed. Summing gives the
        total tokens *sent/billed* across the attempt — the right economic
        measure even though earlier turns' context overlaps.

        ``cost_usd`` is summed only when every contributing message reports a
        cost; otherwise it is ``None`` (never fabricated from partial data).
        """
        # Backward compat: single dict or None → wrap in a list.
        if messages_or_single is None:
            return None
        if isinstance(messages_or_single, dict):
            messages: list[dict[str, Any]] = [messages_or_single]
        else:
            messages = messages_or_single

        assistants = [m for m in messages if m.get("role") == "assistant"]
        # Backward compat: if the list has a single dict without "role"
        # (legacy callers pass a bare assistant dict), treat it as one message.
        if not assistants and len(messages) == 1:
            assistants = messages

        if not assistants:
            return None
        total_in: int | None = None
        total_out: int | None = None
        total_cost: float | None = None
        all_have_cost = True
        turns = 0
        for msg in assistants:
            raw = msg.get("usage")
            if not isinstance(raw, dict):
                continue
            inp, out, cost = PrimeHeadlessExecutor._extract_usage_single(raw)
            if inp is None and out is None:
                continue
            turns += 1
            if inp is not None:
                total_in = (total_in or 0) + inp
            if out is not None:
                total_out = (total_out or 0) + out
            if cost is not None:
                total_cost = (total_cost or 0.0) + cost
            else:
                all_have_cost = False
        if total_in is None and total_out is None:
            return None
        return AttemptUsage(
            input_tokens=total_in,
            output_tokens=total_out,
            cost_usd=total_cost if all_have_cost else None,
            tokens_source="prime_stdout",
            turns=turns if turns > 0 else None,
        )

    def _interpret(
        self, *, stdout: str, stderr: str, returncode: int, route_id: str, duration: float
    ) -> WorkerTerminal:
        values, parsed_any = _iter_json_values(stdout)
        messages = _collect_messages(values)
        assistant: dict[str, Any] | None = None
        for message in messages:
            if message.get("role") == "assistant":
                assistant = message
        usage = self._extract_usage(messages)
        if assistant is None:
            if returncode != 0:
                tail = _sanitize(stderr) or f"exit {returncode} with no assistant message"
                return WorkerTerminal(
                    executor_kind="live",
                    ok=False,
                    model="",
                    error=tail,
                    duration_seconds=duration,
                    usage=usage,
                )
            if not parsed_any:
                return WorkerTerminal(
                    executor_kind="live",
                    ok=False,
                    model="",
                    error=f"malformed: {_sanitize(stdout, 200) or 'empty stdout'}",
                    duration_seconds=duration,
                    usage=usage,
                )
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error="no_final_answer",
                duration_seconds=duration,
                usage=usage,
            )

        reported_model = str(assistant.get("model", ""))
        reported_provider = str(assistant.get("provider", ""))
        stop_reason = str(assistant.get("stopReason", ""))
        text = _text_of(assistant)

        if reported_provider != self.provider or reported_model != route_id:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="model_mismatch",
                duration_seconds=duration,
                usage=usage,
            )
        error_message = assistant.get("errorMessage")
        if error_message:
            error_text = _sanitize(str(error_message))
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error=error_text,
                status_code=_parse_status_code(error_text),
                duration_seconds=duration,
                usage=usage,
            )
        if stop_reason not in {"stop", "end_turn"}:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="no_final_answer",
                duration_seconds=duration,
                usage=usage,
            )
        if not text.strip():
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="empty_output",
                duration_seconds=duration,
                usage=usage,
            )
        return WorkerTerminal(
            executor_kind="live",
            ok=True,
            output=text,
            model=reported_model,
            stop_reason=stop_reason,
            duration_seconds=duration,
            usage=usage,
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
        return WorkerTerminal(
            executor_kind="fault-injected",
            ok=True,
            output="",
            model=route_id,
            session_ref=session_ref,
        )
    if kind not in errors:
        raise ValueError(f"unknown fault kind {kind!r}")
    return WorkerTerminal(
        executor_kind="fault-injected",
        ok=False,
        model=route_id,
        session_ref=session_ref,
        error=errors[kind],
        status_code=codes.get(kind),
        retry_after_seconds=30 if kind == "rate_limit" else None,
    )


_WORKER_ATTEMPT_DIR_RE = re.compile(r"^.+-a\d+$")


class FaultInjectingExecutor:
    """Pop queued synthetic faults per route before delegating to ``inner``."""

    def __init__(self, inner: WorkerExecutor, faults: Mapping[str, list[str]]) -> None:
        self.inner = inner
        self._faults: dict[str, list[str]] = {k: list(v) for k, v in faults.items()}
        self._dispatches = 0  # executor calls seen (planning included), for "#N" keys
        self._worker_dispatches = 0  # attempt worktrees ``<node>-a<N>`` only, for "worker#N"

    @staticmethod
    def _is_worker_attempt(cwd: Path) -> bool:
        """True when ``cwd`` is a node attempt worktree named ``<node>-a<N>``."""
        return _WORKER_ATTEMPT_DIR_RE.fullmatch(cwd.name) is not None

    def _keys(self, route_id: str, cwd: Path) -> list[str]:
        """Match order: #N, worker#N (workers only), route, provider, @node, *.

        The node id is taken from the attempt worktree name ``<node>-a<N>`` so a
        chaos run can target "the first attempt of node X, whatever route the
        eligibility ladder picked" without predicting ranking.

        ``#N`` counts every executor call, including planner and plan-repair.
        ``worker#N`` counts only worker dispatches (attempt worktree ``<node>-a<N>``).
        """
        provider = route_id.split("/", 1)[0] + "/*"
        node = cwd.name.rsplit("-a", 1)[0] if "-a" in cwd.name else cwd.name
        keys = [f"#{self._dispatches}"]
        if self._is_worker_attempt(cwd):
            keys.append(f"worker#{self._worker_dispatches}")
        keys.extend([route_id, provider, "@" + node, "*"])
        return keys

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
        if self._is_worker_attempt(cwd):
            self._worker_dispatches += 1
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
        terminal = result if isinstance(result, WorkerTerminal) else await result
        return replace(terminal, executor_kind="scripted")


# ---------------------------------------------------------------- direct gateway


class DirectGatewayExecutor:
    """Run one prompt on one exact OmniRoute route via direct HTTP — no Prime harness.

    Text-only nodes (research/review/plan) return the model's text content.
    Implement-type nodes request a unified diff for owned files, apply it with
    ``git apply --check`` then ``git apply`` inside the node worktree, and fail
    closed with a named error if it does not apply or touches files outside
    ``owned_files``.

    Failures (4xx/5xx/timeout/empty) are mapped to :class:`WorkerTerminal`
    fields compatible with the recovery classifier so that failover works
    unchanged.
    """

    _OWNED_FILES_RE = re.compile(r"^OWNED_FILES:\s*(.+)$", re.MULTILINE)
    _DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)\n(.*?)```", re.DOTALL)
    # Match --- a/path and +++ b/path lines in unified diffs
    _DIFF_PATH_RE = re.compile(r"^[-+]{3}\s+[ab]/(.+)$", re.MULTILINE)

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:20128",
        api_key: str | None = None,
        timeout_connect: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout_connect = timeout_connect

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _parse_owned_files(prompt: str) -> list[str]:
        """Extract the OWNED_FILES list from a hydrated node prompt."""
        m = DirectGatewayExecutor._OWNED_FILES_RE.search(prompt)
        if not m:
            return []
        raw = m.group(1).strip()
        if raw == "(none)":
            return []
        return [f.strip() for f in raw.split(",") if f.strip()]

    @staticmethod
    def _extract_diff(text: str) -> str | None:
        """Extract a unified diff from model output (fenced or raw)."""
        # Try fenced ```diff ... ``` first
        m = DirectGatewayExecutor._DIFF_FENCE_RE.search(text)
        if m:
            return m.group(1).strip()
        # Fallback: look for raw unified diff (starts with --- or diff --git)
        lines = text.split("\n")
        diff_lines: list[str] = []
        in_diff = False
        for line in lines:
            if line.startswith("diff --git ") or (line.startswith("--- ") and not in_diff):
                in_diff = True
            if in_diff:
                diff_lines.append(line)
        if diff_lines:
            return "\n".join(diff_lines)
        return None

    @staticmethod
    def _validate_diff_paths(diff_text: str, owned_files: list[str]) -> str | None:
        """Return an error string if the diff touches files outside owned_files."""
        paths = DirectGatewayExecutor._DIFF_PATH_RE.findall(diff_text)
        owned_set = set(owned_files)
        for p in paths:
            # Reject absolute paths
            if p.startswith("/"):
                return f"absolute path in diff: {p}"
            # Reject path traversal
            if ".." in p.split("/"):
                return f"path traversal in diff: {p}"
            # /dev/null is allowed (new file creation or deletion)
            if p == "dev/null":
                continue
            if p not in owned_set:
                return f"path outside owned_files: {p}"
        return None

    @staticmethod
    def _normalize_path(p: str) -> str | None:
        """Normalize a diff path; return None if invalid."""
        # Strip leading a/ or b/ prefix (standard git diff format)
        for prefix in ("a/", "b/"):
            if p.startswith(prefix):
                p = p[len(prefix) :]
                break
        normed = posixpath.normpath(p)
        # Reject empty, absolute, traversal, .git paths
        if not normed or normed == ".":
            return None
        if normed.startswith("/"):
            return None
        parts = normed.split("/")
        if ".." in parts:
            return None
        if parts[0] == ".git" or ".git" in parts:
            return None
        return normed

    async def _validate_diff_security(
        self, diff_text: str, owned_files: list[str], cwd: Path
    ) -> str | None:
        """Use git to discover ALL paths a patch touches; reject unsafe ops.

        Returns an error string if the diff is unsafe, None if safe.
        This is the primary security gate — the regex-based
        ``_validate_diff_paths`` is kept as defence in depth.
        """
        owned_set = {posixpath.normpath(f) for f in owned_files}

        # Write the diff to a temp file for git commands
        diff_path = cwd / ".verdict-validate.patch"
        try:
            diff_path.write_text(diff_text + "\n", encoding="utf-8")
        except OSError as exc:
            return f"cannot write patch for validation: {exc}"

        try:
            # --- Step 1: git apply --summary to detect dangerous operations ---
            proc = await asyncio.create_subprocess_exec(
                "git",
                "apply",
                "--summary",
                str(diff_path),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            summary = stdout.decode("utf-8", errors="replace")
            # --summary outputs lines like:
            #   rename owned.py => evil.py (100%)
            #   copy owned.py => evil.py (100%)
            #   mode change 100644 => 100755 file.py
            #   create mode 100644 file.py
            #   delete mode 100644 file.py
            #   create mode 120000 link (symlink)
            #   create mode 160000 sub  (submodule)
            for line in summary.splitlines():
                line_lower = line.strip().lower()
                if line_lower.startswith("rename "):
                    return f"diff_rejected: rename operation not allowed: {line.strip()}"
                if line_lower.startswith("copy "):
                    return f"diff_rejected: copy operation not allowed: {line.strip()}"
                if "mode change" in line_lower:
                    return f"diff_rejected: mode change not allowed: {line.strip()}"
                # Symlink: mode 120000
                if "120000" in line:
                    return f"diff_rejected: symlink creation not allowed: {line.strip()}"
                # Submodule: mode 160000
                if "160000" in line:
                    return f"diff_rejected: submodule entry not allowed: {line.strip()}"
                # Deletion
                if line_lower.startswith("delete "):
                    return f"diff_rejected: file deletion not allowed: {line.strip()}"

            # --- Step 1b: reject binary patches ---
            if "GIT binary patch" in diff_text or "Binary files " in diff_text:
                return "diff_rejected: binary content not allowed"

            # --- Step 2: git apply --numstat -z to collect ALL touched paths ---
            proc = await asyncio.create_subprocess_exec(
                "git",
                "apply",
                "--numstat",
                "-z",
                str(diff_path),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace")[-300:]
                return f"diff_rejected: git cannot parse patch: {err_msg}"

            raw = stdout.decode("utf-8", errors="replace")
            # --numstat -z output: records separated by NUL.
            # Each record: "added\tdeleted\tpath" for normal files,
            # or "added\tdeleted\t" + NUL + "from" + NUL + "to" for renames.
            # Split on NUL and process.
            parts = [p for p in raw.split("\0") if p.strip()]
            touched_paths: list[str] = []
            for part in parts:
                if "\t" in part:
                    # "added\tdeleted\tpath" — extract path after last tab
                    fields = part.split("\t")
                    if len(fields) >= 3 and fields[2]:
                        touched_paths.append(fields[2])
                else:
                    # Standalone path (rename/copy source or dest)
                    touched_paths.append(part)

            # Also parse diff --git headers for rename/copy source/dest
            # (belt-and-suspenders: catches cases --numstat might miss)
            for line in diff_text.splitlines():
                if line.startswith("rename from ") or line.startswith("rename to "):
                    p = line.split(" ", 2)[-1].strip()
                    touched_paths.append(p)
                if line.startswith("copy from ") or line.startswith("copy to "):
                    p = line.split(" ", 2)[-1].strip()
                    touched_paths.append(p)

            # Normalize and validate every path
            for raw_path in touched_paths:
                normed = self._normalize_path(raw_path)
                if normed is None:
                    return f"diff_rejected: invalid path: {raw_path!r}"
                if normed not in owned_set:
                    return f"diff_rejected: path outside owned_files: {normed}"

            # If numstat returned NO paths but the diff has content,
            # that's suspicious (could be mode-only or other exotic format)
            if not touched_paths and diff_text.strip() and "diff --git " in diff_text:
                return "diff_rejected: patch touches no files (mode-only or exotic format)"

        except asyncio.TimeoutError:
            return "diff_rejected: git validation timed out"
        except OSError as exc:
            return f"diff_rejected: git validation error: {exc}"
        finally:
            diff_path.unlink(missing_ok=True)

        return None

    @staticmethod
    def _augment_prompt_for_diff(
        prompt: str, owned_files: list[str], cwd: Path, budget_bytes: int = 60_000
    ) -> str:
        """Append diff-mode instructions and current file contents to the prompt."""
        parts: list[str] = [prompt]
        # Replace the generic RULES block's edit instruction with diff-specific one
        parts.append("")
        parts.append("OUTPUT_FORMAT: unified diff")
        parts.append("You MUST output your changes as a single unified diff (git diff format).")
        parts.append("Wrap the diff in a ```diff fenced code block.")
        parts.append("The diff must use a/ and b/ prefixes (standard git diff format).")
        parts.append("Only modify files listed in OWNED_FILES. Never include paths outside them.")
        parts.append("Do NOT output any other file contents or edits outside the diff block.")
        parts.append("")
        parts.append("CURRENT FILE CONTENTS (for reference):")
        remaining = budget_bytes
        for rel in owned_files:
            path = cwd / rel
            if not path.exists():
                parts.append(f"--- {rel} (does not exist yet — new file) ---")
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                parts.append(f"--- {rel} (unreadable: {exc}) ---")
                continue
            keep = min(len(data), remaining)
            if keep <= 0:
                parts.append(f"--- {rel} (omitted: budget exhausted) ---")
                continue
            text = data[:keep].decode("utf-8", errors="replace")
            parts.append(f"--- {rel} ---")
            parts.append(text)
            if keep < len(data):
                parts.append(f"[TRUNCATED: {len(data) - keep} bytes omitted]")
            remaining -= keep
        return "\n".join(parts)

    async def _apply_diff(self, diff_text: str, cwd: Path) -> tuple[bool, str]:
        """Run git apply --check then git apply. Returns (ok, error_detail)."""
        # Write diff to a temp file
        diff_path = cwd / ".verdict-pending.patch"
        try:
            diff_path.write_text(diff_text + "\n", encoding="utf-8")
        except OSError as exc:
            return False, f"cannot write patch file: {exc}"
        try:
            # --check first (dry run)
            proc = await asyncio.create_subprocess_exec(
                "git",
                "apply",
                "--check",
                str(diff_path),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                tail = stderr.decode("utf-8", errors="replace")[-300:]
                return False, f"git apply --check failed: {tail}"
            # Apply for real
            proc = await asyncio.create_subprocess_exec(
                "git",
                "apply",
                "--whitespace=nowarn",
                str(diff_path),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                tail = stderr.decode("utf-8", errors="replace")[-300:]
                return False, f"git apply failed: {tail}"
            return True, ""
        except asyncio.TimeoutError:
            return False, "git apply timed out"
        except OSError as exc:
            return False, f"git apply error: {exc}"
        finally:
            diff_path.unlink(missing_ok=True)

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        started = time.monotonic()
        owned_files = self._parse_owned_files(prompt)
        is_implement = bool(owned_files)

        # For implement nodes, augment the prompt to request a unified diff
        effective_prompt = prompt
        if is_implement:
            effective_prompt = self._augment_prompt_for_diff(prompt, owned_files, cwd)

        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": route_id,
            "messages": [{"role": "user", "content": effective_prompt}],
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error="timeout",
                duration_seconds=time.monotonic() - started,
            )
        except (httpx.TransportError, OSError) as exc:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error=f"transport: {_sanitize(str(exc), 200)}",
                duration_seconds=time.monotonic() - started,
            )
        duration = time.monotonic() - started

        # Parse the HTTP response into a WorkerTerminal
        terminal = self._interpret(resp, route_id=route_id, duration=duration)

        # For non-implement nodes or failed HTTP, return as-is
        if not is_implement or not terminal.ok:
            return terminal

        # --- Implement node: extract, validate, and apply the diff ---
        diff_text = self._extract_diff(terminal.output)
        if diff_text is None:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                output=terminal.output,
                model=terminal.model,
                error="diff_rejected: no unified diff found in model output",
                duration_seconds=terminal.duration_seconds,
                session_ref=terminal.session_ref,
                usage=terminal.usage,
            )

        # Primary security gate: git-based path + operation validation
        security_error = await self._validate_diff_security(diff_text, owned_files, cwd)
        if security_error is not None:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                output=terminal.output,
                model=terminal.model,
                error=security_error
                if security_error.startswith("diff_rejected:")
                else f"diff_rejected: {security_error}",
                duration_seconds=terminal.duration_seconds,
                session_ref=terminal.session_ref,
                usage=terminal.usage,
            )

        # Defence in depth: regex-based path check
        path_error = self._validate_diff_paths(diff_text, owned_files)
        if path_error is not None:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                output=terminal.output,
                model=terminal.model,
                error=f"diff_rejected: {path_error}",
                duration_seconds=terminal.duration_seconds,
                session_ref=terminal.session_ref,
                usage=terminal.usage,
            )

        # Apply the diff
        applied, apply_error = await self._apply_diff(diff_text, cwd)
        if not applied:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                output=terminal.output,
                model=terminal.model,
                error=f"diff_apply_failed: {apply_error}",
                duration_seconds=terminal.duration_seconds,
                session_ref=terminal.session_ref,
                usage=terminal.usage,
            )

        return terminal

    @staticmethod
    def _interpret(resp: httpx.Response, *, route_id: str, duration: float) -> WorkerTerminal:
        """Map an OpenAI-compatible chat/completions response to WorkerTerminal."""
        if resp.status_code >= 400:
            error_text = _sanitize(resp.text, 400)
            retry_after: float | None = None
            raw_retry = resp.headers.get("retry-after")
            if raw_retry:
                with contextlib.suppress(ValueError):
                    retry_after = float(raw_retry)
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error=error_text,
                status_code=resp.status_code,
                retry_after_seconds=retry_after,
                duration_seconds=duration,
            )
        try:
            body = resp.json()
        except (ValueError, TypeError):
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error=f"malformed: {_sanitize(resp.text, 200)}",
                duration_seconds=duration,
            )
        choices = body.get("choices") or []
        if not choices:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model="",
                error="empty_output",
                duration_seconds=duration,
            )
        choice = choices[0]
        message = choice.get("message", {})
        text = message.get("content", "")
        finish = choice.get("finish_reason", "")
        stop_reason = "end_turn" if finish in {"stop", "end_turn"} else finish

        # Extract usage
        raw_usage = body.get("usage")
        usage: AttemptUsage | None = None
        if isinstance(raw_usage, dict):
            inp = raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens")
            out = raw_usage.get("completion_tokens") or raw_usage.get("output_tokens")
            cost = raw_usage.get("cost")
            if inp is not None or out is not None:
                usage = AttemptUsage(
                    input_tokens=int(inp) if inp is not None else None,
                    output_tokens=int(out) if out is not None else None,
                    cost_usd=float(cost) if cost is not None else None,
                    tokens_source="http_response",
                    turns=1,
                )

        reported_model = body.get("model", route_id)

        if stop_reason not in {"end_turn", "stop", ""}:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=reported_model,
                stop_reason=stop_reason,
                error="no_final_answer",
                duration_seconds=duration,
                usage=usage,
            )

        if not text.strip():
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=reported_model,
                error="empty_output",
                duration_seconds=duration,
                usage=usage,
            )

        return WorkerTerminal(
            executor_kind="live",
            ok=True,
            output=text,
            model=reported_model,
            stop_reason=stop_reason or "end_turn",
            duration_seconds=duration,
            session_ref=f"direct-gateway:{body.get('id', '')}",
            usage=usage,
        )


# ----------------------------------------------------------------- mixed


class MixedExecutor:
    """Route each node to a named executor from an explicit map.

    The map key is a node id (e.g. ``setup-db``).  Calls that do not match any
    key — planning, plan-repair, or unmapped worker nodes — fall back to
    ``default``.

    Node id is extracted from ``cwd``: attempt worktrees are named
    ``<node_id>-a<N>`` by the runtime; non-attempt ``cwd`` values (planning
    calls whose ``cwd`` is the bare repo root) map to the default.

    The ``executor_kind`` on each returned :class:`WorkerTerminal` is stamped
    with the *logical backend name* for that slot (e.g. ``"prime-headless"`` or
    ``"direct-gateway"``), overriding the delegate's own kind.  This ensures
    cockpit and receipt always show which named harness ran each node.

    Usage (via ``--executor-map``)::

        verdict orchestrate "goal" \
            --executor-map "node-1=prime,node-2=direct-gateway"
    """

    _BACKEND_KINDS: ClassVar[dict[str, str]] = {
        "prime": "prime-headless",
        "direct-gateway": "direct-gateway",
    }

    def __init__(
        self,
        node_map: dict[str, WorkerExecutor],
        default: WorkerExecutor,
        *,
        node_kind_map: dict[str, str] | None = None,
        default_kind: str = "",
    ) -> None:
        self._map = dict(node_map)
        self._default = default
        # kind_map maps node_id -> executor_kind label to stamp on terminals.
        self._kind_map: dict[str, str] = dict(node_kind_map or {})
        self._default_kind = default_kind

    @staticmethod
    def _node_id_from_cwd(cwd: Path) -> str | None:
        """Return the node id when *cwd* is an attempt worktree, else None."""
        name = cwd.name
        if "-a" in name:
            # Attempt worktrees are named ``<node_id>-aN``; strip suffix.
            parts = name.rsplit("-a", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return parts[0]
        return None

    def _pick(self, cwd: Path) -> tuple[WorkerExecutor, str]:
        """Return ``(executor, kind_label)`` for this call."""
        node_id = self._node_id_from_cwd(cwd)
        if node_id is not None and node_id in self._map:
            return self._map[node_id], self._kind_map.get(node_id, "")
        return self._default, self._default_kind

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        executor, kind = self._pick(cwd)
        terminal = await executor.run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )
        if kind:
            terminal = replace(terminal, executor_kind=kind)
        return terminal
