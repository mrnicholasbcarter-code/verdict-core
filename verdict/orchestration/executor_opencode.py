"""OpenCode worker executor for Verdict orchestration.

Runs one task on one exact OmniRoute route through OpenCode's non-interactive
dispatch (``opencode run --standalone --auto --format json --archived``).

Each attempt:

1. Writes a per-attempt ``opencode.json`` to a temp directory, pointed at by
   ``OPENCODE_CONFIG_DIR``.
2. Writes ``.verdict-context.md`` to the worktree as ambient instructions.
3. Launches the opencode binary and collects NDJSON stdout.
4. Maps the result to :class:`~verdict.orchestration.contracts.WorkerTerminal`.
5. Deletes the temp directory on exit.

**DISPATCH_ORIGIN_CONFLICT** — this executor declares
:data:`~verdict.orchestration.contracts.LOCAL_EXECUTOR_ORIGIN` as a documented
stopgap.  OpenCode workers route through Verdict serve (:8000), but
``DispatchAuthority.gateway_origin`` is set from ``--gateway`` (OmniRoute
:20128).  Reporting ``http://127.0.0.1:8000`` would fail the dispatch origin
check in ``runtime.py`` (line ~1772).  Route identity is enforced by serve's
per-request relay pinning instead (Decision 1, Task 2.4).  Follow-on: add a
``worker_serve_origin`` field to ``DispatchAuthority`` so OpenCode can declare
its real origin and the runtime accepts it without using LOCAL_EXECUTOR_ORIGIN.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import LOCAL_EXECUTOR_ORIGIN, AttemptUsage, WorkerTerminal
from verdict.orchestration.executors import idle_output_seconds_from_env

__all__ = ["PINNED_VERSION", "OpenCodeExecutor"]

#: Pinned OpenCode version for this adapter.  ``verdict doctor`` warns on mismatch.
PINNED_VERSION = "2.3.2"
#: Env var to override the opencode binary path.
OPENCODE_BIN_ENV = "VERDICT_OPENCODE_BIN"
#: Env var the per-attempt config references for the worker API key.
WORKER_API_KEY_ENV = "VERDICT_WORKER_API_KEY"
#: Default Verdict serve base URL for the per-attempt provider config.
SERVE_BASE_URL_DEFAULT = "http://127.0.0.1:8000"
#: Context file written to the worktree before each attempt (Task 4.1).
CONTEXT_FILE_NAME = ".verdict-context.md"

_TERM_GRACE_SECONDS = 5.0
_STDERR_TAIL_CHARS = 400
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_STATUS_RE = re.compile(r"\b([45]\d{2})\b")


#: Marker that replaces a secret in any text taken from the worker subprocess.
REDACTED = "[REDACTED]"
_AUTH_BEARER_RE = re.compile(
    r"""(authorization["']?\s*[:=]\s*["']?bearer\s+)[^\s"',;]+""", re.IGNORECASE
)


def _redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace each secret value and any ``Authorization: Bearer <v>`` value.

    The worker subprocess holds the serve token in its environment, so anything
    it echoes (stderr, NDJSON ``error`` and ``text`` events) may contain it.
    Longest secrets are replaced first so a secret that contains another is
    removed whole.
    """
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _AUTH_BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)


def _sanitize(text: str, limit: int = _STDERR_TAIL_CHARS, secrets: Iterable[str] = ()) -> str:
    """Strip control characters, redact secrets, then keep the last ``limit`` chars.

    Redaction happens before truncation: truncating first could cut a secret in
    half and leave a fragment that an exact-match replacement no longer finds.
    """
    clean = _redact(_CONTROL_RE.sub("", text), secrets).strip()
    return clean[-limit:]


def _parse_status_code(message: str) -> int | None:
    match = _STATUS_RE.search(message)
    return int(match.group(1)) if match else None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _attempt_session_id() -> str:
    """A session ID that opencode accepts (must start with ``ses_``)."""
    return f"ses_{uuid.uuid4().hex[:20]}"


def _opencode_binary(env: dict[str, str] | None = None) -> str:
    """Resolve the opencode binary path.

    Precedence: ``VERDICT_OPENCODE_BIN`` env override → ``~/.opencode/bin/opencode``
    → ``PATH`` → bare name fallback.
    """
    source = env if env is not None else dict(os.environ)
    override = source.get(OPENCODE_BIN_ENV, "").strip()
    if override:
        return override
    home = source.get("HOME", os.path.expanduser("~"))
    candidate = Path(home) / ".opencode" / "bin" / "opencode"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("opencode", path=source.get("PATH"))
    if found:
        return found
    return "opencode"


def _write_attempt_config(
    config_dir: Path, *, route_id: str, serve_base_url: str, api_key_env: str, cwd: Path
) -> None:
    """Write a minimal per-attempt opencode.json to ``config_dir`` (Task 2.3).

    The API key value is never written to disk — only the env var reference
    ``{env:<api_key_env>}`` that opencode expands at runtime.

    ``instructions`` reference ``.verdict-context.md`` in the worktree for
    ambient task context (Task 4.1).  When an ``AGENTS.md`` file is present in
    ``cwd`` it is also listed, so the worker sees project coding conventions.

    ``headers`` includes ``X-Verdict-Worker: opencode`` so that ``verdict
    serve``'s relay can identify worker calls and apply per-request route
    pinning without requiring ``LLMGATE_ALLOW_CLIENT_MODEL_OVERRIDE`` to be
    set globally.

    ``policies`` deny all ``provider.use`` actions except ``verdict`` so the
    worker cannot call openrouter, bedrock, or any other provider directly.
    ``--auto`` mode respects explicit deny policies.

    ``permissions`` deny ``external_directory`` (V2 action name; last-rule-wins
    semantics in V2). This prevents the worker from opening directories outside
    the worktree as project roots.  Verdict's post-attempt owned-files check and
    the diff-containment gate are the primary enforcement layer.

    ``agents.title.model`` is pinned to the same admitted route so the title
    summariser cannot pick its own model and bypass the route decision.
    """
    instructions: list[str] = [str(cwd / CONTEXT_FILE_NAME)]
    agents_md = cwd / "AGENTS.md"
    if agents_md.is_file():
        instructions.append(str(agents_md))
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "providers": {
            "verdict": {
                "name": "Verdict",
                "package": "aisdk:@ai-sdk/openai-compatible",
                "settings": {
                    "baseURL": f"{serve_base_url.rstrip('/')}/v1",
                    "apiKey": f"{{env:{api_key_env}}}",
                },
                # Worker marker — lets serve relay enable route pinning for
                # this request without a global env override.
                "headers": {"X-Verdict-Worker": "opencode"},
                "models": {route_id: {"name": route_id, "capabilities": {"tools": True}}},
            }
        },
        # Deny all provider.use except verdict (ADR-053 Decision 1).
        # Prevents the worker from calling openrouter/bedrock/etc. directly.
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "verdict", "effect": "allow"},
        ],
        # V2: last matching rule wins; external_directory deny prevents the
        # worker from opening directories outside the node worktree as roots.
        "permissions": [{"action": "external_directory", "resource": "*", "effect": "deny"}],
        # Pin the title agent to the same admitted route (ADR-053 Decision 1).
        "agents": {"title": {"model": f"verdict/{route_id}"}},
        "instructions": instructions,
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = config_dir / "opencode.json"
    cfg_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _write_context_file(cwd: Path, prompt: str) -> None:
    """Write ``.verdict-context.md`` to the worktree for OpenCode's instructions.

    The file is listed in ``instructions`` in the per-attempt config so opencode
    loads it as ambient context.  It is excluded from the ownership diff by the
    ``.verdict-context.md`` entry in ``BYPRODUCT_EXCLUDES`` (runtime.py), which
    ``_ensure_local_excludes`` appends to ``.git/info/exclude`` before every
    ``git add -A``.  Since it is excluded, it is never staged and never appears
    in the changed-files list that the ownership check compares against
    ``node.owned_files``.
    """
    content = "<!-- Verdict worker context — do not commit -->\n# Task\n\n" + prompt + "\n"
    # The worktree is repository content and so untrusted: a repo can track
    # ``.verdict-context.md`` as a symlink to a path outside the worktree.
    # ``Path.write_text`` would follow it and overwrite that target with the
    # orchestrator's permissions.  Instead write a new regular file (``mkstemp``
    # uses O_EXCL, so it never follows a link) and ``os.replace`` it over the
    # entry: rename replaces the symlink itself and never touches its target.
    target = cwd / CONTEXT_FILE_NAME
    fd, tmp_name = tempfile.mkstemp(dir=cwd, prefix=f"{CONTEXT_FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


_CHILD_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "LANG",
        "TZ",
        "TMPDIR",
        "OPENCODE_CONFIG_DIR",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "OPENCODE_CONFIG_PROJECT_DISABLE",
        "OPENCODE_DISABLE_CLAUDE_CODE",
    }
)
_CHILD_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_",)


def _build_child_env(
    *,
    config_dir: Path,
    data_dir: Path,
    state_dir: Path,
    api_key_env: str,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build an isolated environment for the opencode subprocess.

    Isolation strategy (Task 2.3):
    - ``OPENCODE_CONFIG_DIR`` → per-attempt config directory (no global config
      merge, no MCP servers, no foreign providers leak in).
    - ``XDG_DATA_HOME`` → per-attempt data directory (session DB and history).
    - ``XDG_STATE_HOME`` → per-attempt state directory (locks, runtime state).
    - ``OPENCODE_CONFIG_PROJECT_DISABLE=1`` → prevents project-level
      ``opencode.json`` in the cwd from merging with the per-attempt config.
    - ``OPENCODE_DISABLE_CLAUDE_CODE=1`` → prevents OpenCode v2.3.2 from
      loading the operator's ``~/.claude`` config and skills.

    Credential hygiene (belt-and-braces):
    The child env is built from an explicit allowlist (``_CHILD_ENV_ALLOWLIST``
    plus ``LC_*`` prefixed vars), not by stripping individual keys.  This
    prevents ``*_API_KEY``, ``*_TOKEN``, ``AWS_*`` and any other provider
    credential from reaching the worker.  ``VERDICT_WORKER_API_KEY`` (or the
    configured ``api_key_env``) is explicitly allowed so the
    ``{env:...}`` reference in the per-attempt config can expand at runtime.
    """
    source = dict(base_env if base_env is not None else os.environ)
    env: dict[str, str] = {}
    for key, val in source.items():
        if (
            key == api_key_env
            or key in _CHILD_ENV_ALLOWLIST
            or any(key.startswith(p) for p in _CHILD_ENV_ALLOWLIST_PREFIXES)
        ):
            env[key] = val
    env["OPENCODE_CONFIG_DIR"] = str(config_dir)
    env["XDG_DATA_HOME"] = str(data_dir)
    env["XDG_STATE_HOME"] = str(state_dir)
    env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    return env


def _parse_ndjson_output(stdout: str, secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Parse NDJSON output from ``opencode run --format json``.

    OpenCode emits one JSON object per line.  Interesting event types:

    - ``text`` — incremental text delta (not the full assistant output).
    - ``step_finish`` — end of one model step; carries ``tokens`` for usage
      and ``reason`` (stop reason).
    - ``error`` — error message; non-zero exit always takes precedence.

    Every string taken from the stream (error text, assistant text, session id)
    has ``secrets`` and ``Authorization: Bearer`` values redacted after the JSON
    is decoded, so JSON escaping cannot hide a secret from the replacement and a
    secret split across ``text`` deltas is caught once the deltas are joined.

    Returns a dict with keys: ``usage``, ``session_id``, ``last_text``,
    ``error_message``.
    """
    session_id = ""
    text_parts: list[str] = []
    error_message = ""

    total_input: int | None = None
    total_output: int | None = None
    total_cost: float | None = None
    all_have_cost = True
    has_usage = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        sid = event.get("sessionID", "")
        if isinstance(sid, str) and sid and not session_id:
            session_id = sid

        event_type = event.get("type", "")

        if event_type == "text":
            part = event.get("text", "")
            if isinstance(part, str):
                text_parts.append(part)

        elif event_type == "step_finish":
            tokens = event.get("tokens") or {}
            if isinstance(tokens, dict):
                inp = _as_int(tokens.get("input"))
                out = _as_int(tokens.get("output"))
                cost_raw = event.get("cost")
                cost_val = (
                    _as_float(cost_raw.get("total"))
                    if isinstance(cost_raw, dict)
                    else _as_float(cost_raw)
                )
                if inp is not None:
                    total_input = (total_input or 0) + inp
                    has_usage = True
                if out is not None:
                    total_output = (total_output or 0) + out
                    has_usage = True
                if cost_val is not None:
                    total_cost = (total_cost or 0.0) + cost_val
                else:
                    all_have_cost = False

        elif event_type == "error":
            msg = event.get("message") or event.get("error") or ""
            if isinstance(msg, str) and msg:
                error_message = _sanitize(msg, secrets=secrets)

    usage: AttemptUsage | None = None
    if has_usage:
        usage = AttemptUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            cost_usd=total_cost if all_have_cost else None,
            tokens_source="opencode_ndjson",
        )

    return {
        "usage": usage,
        "session_id": _redact(session_id, secrets),
        "last_text": _redact("".join(text_parts), secrets),
        "error_message": error_message,
    }


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the process group; always escalate to SIGKILL after the grace period.

    A direct child that exits within the grace period does not guarantee the
    process group is empty: a descendant that ignores SIGTERM and redirects its
    pipes can survive and keep editing the worktree.  Sending SIGKILL to the
    recorded process group unconditionally after the grace period closes this
    gap, whether or not the leader is still alive.

    The subprocess is always started with ``start_new_session=True``, which
    makes it both session leader and process group leader, so its PID is its
    process group ID for life (a session leader cannot change its pgid).  The
    group persists as long as any member (e.g. a grandchild) is alive, even
    after the leader is reaped, and the kernel does not reuse a PID that is
    still in use as a process group ID.  So ``proc.pid`` is always the right
    target; looking the pgid up through the leader would fail once it is reaped.
    """
    pgid = proc.pid

    def _signal(sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(pgid, sig)

    _signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    finally:
        # Always killpg after the grace period to clean up surviving descendants.
        _signal(signal.SIGKILL)
        if proc.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SECONDS)


class OpenCodeExecutor:
    """Run one task through OpenCode's non-interactive dispatch.

    See module docstring for the DISPATCH_ORIGIN_CONFLICT note and the
    per-attempt isolation strategy.
    """

    # See DISPATCH_ORIGIN_CONFLICT note in module docstring.
    gateway_origin: str = LOCAL_EXECUTOR_ORIGIN

    def __init__(
        self,
        *,
        opencode_bin: str = "",
        serve_base_url: str = SERVE_BASE_URL_DEFAULT,
        api_key_env: str = WORKER_API_KEY_ENV,
        idle_output_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._opencode_bin = opencode_bin
        self._serve_base_url = serve_base_url
        self._api_key_env = api_key_env
        self._env = dict(env) if env is not None else None
        self.idle_output_seconds: float = (
            idle_output_seconds
            if idle_output_seconds is not None
            else idle_output_seconds_from_env(self._base_env())
        )

    def _base_env(self) -> dict[str, str]:
        return dict(self._env) if self._env is not None else dict(os.environ)

    def _resolve_bin(self) -> str:
        if self._opencode_bin:
            return self._opencode_bin
        return _opencode_binary(self._base_env())

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        """Run one task.  Never raises; always returns a terminal."""
        terminal = await self._run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )
        if not terminal.harness:
            terminal = replace(terminal, harness="opencode")
        return terminal

    async def _run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        started = time.monotonic()

        # --- write context file to worktree (Task 4.1) ---
        try:
            _write_context_file(cwd, prompt)
        except OSError as exc:
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=route_id,
                harness="opencode",
                error=f"context_write_failed: {_sanitize(str(exc))}",
                duration_seconds=time.monotonic() - started,
            )

        # --- create per-attempt temp directories (Task 2.3) ---
        attempt_tmp = tempfile.TemporaryDirectory(prefix="verdict-oc-")
        try:
            tmp_path = Path(attempt_tmp.name)
            config_dir = tmp_path / "config"
            data_dir = tmp_path / "data"
            state_dir = tmp_path / "state"

            try:
                _write_attempt_config(
                    config_dir,
                    route_id=route_id,
                    serve_base_url=self._serve_base_url,
                    api_key_env=self._api_key_env,
                    cwd=cwd,
                )
            except OSError as exc:
                return WorkerTerminal(
                    executor_kind="live",
                    ok=False,
                    model=route_id,
                    harness="opencode",
                    error=f"config_write_failed: {_sanitize(str(exc))}",
                    duration_seconds=time.monotonic() - started,
                )

            child_env = _build_child_env(
                config_dir=config_dir,
                data_dir=data_dir,
                state_dir=state_dir,
                api_key_env=self._api_key_env,
                base_env=self._base_env(),
            )

            session_id = _attempt_session_id()
            # ``--model verdict/<route_id>`` selects provider "verdict" and the
            # exact route registered in the per-attempt config (Task 2.2/2.3).
            argv = [
                self._resolve_bin(),
                "run",
                "--standalone",
                "--auto",
                "--format",
                "json",
                "--archived",
                "--session",
                session_id,
                "--model",
                f"verdict/{route_id}",
                "--",
                prompt,
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=child_env,
                    start_new_session=True,
                )
            except OSError as exc:
                return WorkerTerminal(
                    executor_kind="live",
                    ok=False,
                    model=route_id,
                    harness="opencode",
                    error=f"spawn_failed: {_sanitize(str(exc))}",
                    duration_seconds=time.monotonic() - started,
                )

            try:
                stdout_b, stderr_b, cut = await self._collect(proc, timeout_seconds)
                # Always reap the whole process group before returning, on a normal
                # exit as well as on stall/timeout: a descendant with redirected
                # pipes can outlive its leader and keep editing the worktree while
                # owned-files validation runs.
                await _kill_process_group(proc)
                if cut:
                    stalled = cut == "stalled"
                    return WorkerTerminal(
                        executor_kind="live",
                        ok=False,
                        model=route_id,
                        harness="opencode",
                        stop_reason="stalled" if stalled else "",
                        error=(
                            f"stalled: no worker output for {self.idle_output_seconds:g}s"
                            if stalled
                            else "timeout"
                        ),
                        duration_seconds=time.monotonic() - started,
                    )
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await _kill_process_group(proc)
                raise

            duration = time.monotonic() - started
            return self._interpret(
                stdout=stdout_b.decode("utf-8", "replace"),
                stderr=stderr_b.decode("utf-8", "replace"),
                returncode=proc.returncode if proc.returncode is not None else -1,
                route_id=route_id,
                duration=duration,
                session_id=session_id,
            )
        finally:
            attempt_tmp.cleanup()

    async def _collect(
        self, proc: asyncio.subprocess.Process, timeout_seconds: float
    ) -> tuple[bytes, bytes, str]:
        """Collect both pipes with timeout and idle-output stall kill.

        Returns ``(stdout, stderr, cut)``; ``cut`` is ``""`` on normal exit,
        ``"timeout"`` when the whole attempt runs past ``timeout_seconds``, and
        ``"stalled"`` when no output byte arrived for ``idle_output_seconds``.
        """
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        last_output = time.monotonic()

        async def pump(name: str, stream: asyncio.StreamReader | None) -> None:
            nonlocal last_output
            if stream is None:
                return
            while chunk := await stream.read(65536):
                buffers[name] += chunk
                last_output = time.monotonic()

        tasks = {
            asyncio.ensure_future(pump("stdout", proc.stdout)),
            asyncio.ensure_future(pump("stderr", proc.stderr)),
            asyncio.ensure_future(proc.wait()),
        }
        started = time.monotonic()
        cut = ""
        try:
            while True:
                now = time.monotonic()
                hard_left = timeout_seconds - (now - started)
                idle_left = self.idle_output_seconds - (now - last_output)
                if hard_left <= 0:
                    cut = "timeout"
                    break
                if idle_left <= 0:
                    cut = "stalled"
                    break
                _, pending = await asyncio.wait(tasks, timeout=min(hard_left, idle_left))
                if not pending:
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return bytes(buffers["stdout"]), bytes(buffers["stderr"]), cut

    def _interpret(
        self,
        *,
        stdout: str,
        stderr: str,
        returncode: int,
        route_id: str,
        duration: float,
        session_id: str,
    ) -> WorkerTerminal:
        """Map opencode output to WorkerTerminal.

        Exit code is authoritative: 0 → ok=True, non-zero → ok=False.
        NDJSON is parsed for usage (``step_finish`` tokens) and session ID.
        """
        secrets = [self._base_env().get(self._api_key_env, "")]
        parsed = _parse_ndjson_output(stdout, secrets)
        usage = parsed["usage"]
        actual_session_id = parsed["session_id"] or session_id
        text = parsed["last_text"]
        error_message = parsed["error_message"]

        if returncode != 0:
            tail = error_message or _sanitize(stderr, secrets=secrets) or f"exit {returncode}"
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=route_id,
                harness="opencode",
                error=_sanitize(tail),
                status_code=_parse_status_code(tail) if not error_message else None,
                duration_seconds=duration,
                session_ref=actual_session_id,
                usage=usage,
            )

        # Exit 0 is success; agentic runs may produce empty text output when
        # all work is done through tool calls (file edits).  ok=True regardless.
        return WorkerTerminal(
            executor_kind="live",
            ok=True,
            output=text,
            model=route_id,
            harness="opencode",
            stop_reason="stop",
            duration_seconds=duration,
            session_ref=actual_session_id,
            usage=usage,
        )
