"""OpenCode worker executor for Verdict orchestration.

Runs one task on one exact OmniRoute route through OpenCode's non-interactive
dispatch (``opencode run --standalone --auto --format json --archived``).

Each attempt:

0. Resolves the serve credential through :func:`_worker_credential` and checks, once and
   before anything is launched, that the opencode binary runs and that ``verdict serve``
   is reachable and accepts that credential.  Any failure is one
   ``harness_configuration:`` message, which recovery treats as a block, not a retry.
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
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
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
#: Context file written to the worktree before each attempt (Task 4.1).
CONTEXT_FILE_NAME = ".verdict-context.md"

_TERM_GRACE_SECONDS = 5.0
#: Cap on worker output (stdout plus stderr) kept in memory per attempt.  NDJSON is parsed
#: after the run and several attempts run concurrently, so a worker that prints without
#: end must fail its attempt rather than exhaust the orchestrator.
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
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


def _sanitize_full(text: str, secrets: Iterable[str] = ()) -> str:
    """Strip control characters and redact secrets; never truncate.

    For reading a value out of the whole text (an HTTP status) before the part kept for the
    record is cut.  What is recorded must come from :func:`_sanitize`, not from this.
    """
    return _redact(_CONTROL_RE.sub("", text), secrets).strip()


def _sanitize(text: str, limit: int = _STDERR_TAIL_CHARS, secrets: Iterable[str] = ()) -> str:
    """Strip control characters, redact secrets, then keep the last ``limit`` chars.

    Redaction happens before truncation: truncating first could cut a secret in
    half and leave a fragment that an exact-match replacement no longer finds.
    """
    return _sanitize_full(text, secrets)[-limit:]


def _parse_status_code(message: str) -> int | None:
    match = _STATUS_RE.search(message)
    return int(match.group(1)) if match else None


def _status_from_full_text(full: str) -> int | None:
    """The HTTP status in sanitized, untruncated ``full``: its tail first, then the rest.

    The tail (the part that is recorded) is searched first and wins, so reading the whole text
    never changes an answer the tail already gave: a stray number early in a long diagnostic
    cannot beat the real status at its end.  Only when the tail has none is the text before it
    read, which is what keeps a status that a long diagnostic pushed out of the tail.
    """
    return _parse_status_code(full[-_STDERR_TAIL_CHARS:]) or _parse_status_code(full)


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


class _TrackingUnknownError(OSError):
    """Git could not say whether the context file is tracked; callers fail closed."""


class _PreparationDeadlineError(OSError):
    """The attempt's time budget ran out while the context file was being prepared."""


def _git(cwd: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[bytes]:
    """Run git under the process environment, as the runtime's own git calls do.

    ``LC_ALL=C`` only pins the wording of git's messages so a confirmed
    non-repository can be told apart from a failure.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout, check=False, env=env
    )


def _probe_timeout(deadline: float | None) -> float:
    """Timeout for one git probe: 30 s, or what is left of the attempt budget if less."""
    if deadline is None:
        return 30.0
    left = deadline - time.monotonic()
    if left <= 0:
        raise _PreparationDeadlineError("attempt deadline reached while preparing the context file")
    return min(30.0, left)


def _context_path_is_tracked(cwd: Path, deadline: float | None = None) -> bool:
    """True when ``cwd`` is a git worktree whose index tracks the context file.

    Returns False only when that is *known*: git itself reports that ``cwd`` is not
    inside a repository, or git answered and the path is not in the index.  Git
    decides (not a look for ``.git`` on disk), because ``GIT_DIR``/``GIT_WORK_TREE``
    make a worktree with no ``.git`` entry.  Anything indeterminate (git missing,
    timing out, exiting nonzero for another reason, a bare repository, an unreadable
    index) raises ``OSError``: treating it as "untracked" could let a tracked entry
    be replaced and then staged into the node deliverable.

    ``deadline`` (an absolute ``time.monotonic()`` value) bounds the probes: each gets at
    most what is left of it, and ``_PreparationDeadlineError`` is raised once it passes, so
    a hung repository cannot hold an attempt past its own budget.
    """

    def unknown(why: str) -> _TrackingUnknownError:
        return _TrackingUnknownError(
            f"cannot determine whether {CONTEXT_FILE_NAME} is tracked: {why}"
        )

    try:
        probe = _git(cwd, "rev-parse", "--is-inside-work-tree", timeout=_probe_timeout(deadline))
        if probe.returncode != 0:
            if b"not a git repository" in probe.stderr:
                return False  # confirmed: nothing here can be tracked
            raise unknown(f"git exited {probe.returncode}")
        if probe.stdout.strip() != b"true":
            raise unknown("not inside a git work tree")
        done = _git(
            cwd,
            "ls-files",
            "--cached",
            "-z",
            "--",
            CONTEXT_FILE_NAME,
            timeout=_probe_timeout(deadline),
        )
    except (_TrackingUnknownError, _PreparationDeadlineError):
        raise
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and time.monotonic() >= deadline - 0.05:
            raise _PreparationDeadlineError(
                "attempt deadline reached while preparing the context file"
            ) from exc
        raise unknown("TimeoutExpired") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise unknown(type(exc).__name__) from exc
    if done.returncode != 0:
        raise unknown(f"git exited {done.returncode}")
    return bool(done.stdout)


def _write_context_file(cwd: Path, prompt: str, deadline: float | None = None) -> None:
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
    if deadline is not None and time.monotonic() >= deadline:
        raise _PreparationDeadlineError("attempt deadline reached while preparing the context file")
    if _context_path_is_tracked(cwd, deadline):
        # ``.git/info/exclude`` only hides *untracked* paths.  Replacing a tracked
        # entry would be staged by ``git add -A`` into the node deliverable (an
        # ownership failure, or the task prompt committed if the path were owned).
        raise OSError(
            f"{CONTEXT_FILE_NAME} is tracked by git in this worktree; refusing to replace it"
        )
    fd, tmp_name = tempfile.mkstemp(dir=cwd, prefix=f"{CONTEXT_FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


async def _prepare_context(cwd: Path, prompt: str, deadline: float | None = None) -> None:
    """Write the context file off the event loop, without letting a write outlive a cancel.

    The git tracking check can take seconds and the loop is shared by every
    concurrent worker, so the work runs in a thread.  A thread cannot be
    interrupted: if this coroutine is cancelled it waits for the thread to finish
    (the write then either completes or fails) before propagating the cancellation,
    so a caller that has seen the cancellation complete can clean up or reuse the
    worktree without a late write racing it.  ``deadline`` (absolute monotonic time) bounds
    the thread itself, so that wait cannot outlast the attempt's budget by much.
    """
    task = asyncio.ensure_future(asyncio.to_thread(_write_context_file, cwd, prompt, deadline))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # cancelled again: still wait for the thread
            except Exception:
                break  # the preparation failed; the cancellation still wins
        if task.done() and not task.cancelled():
            task.exception()  # mark retrieved: the cancellation is what propagates
        raise


def _worker_credential(route_id: str, attempt_id: str) -> str | None:
    """The bearer credential this attempt's worker presents to ``verdict serve``.

    The single place the worker credential is chosen, so it is easy to swap: today it is
    serve's own bearer token, resolved by Verdict's code (the same sources and order
    ``verdict setup`` uses: environment, credential store, ``serve.env``).  A per-attempt
    signed assignment bound to ``route_id`` and ``attempt_id`` is expected to replace it.
    Never the OmniRoute gateway key.
    """
    from verdict.serve_service import resolve_serve_token

    resolved = resolve_serve_token()
    return resolved[0] if resolved else None


_CREDENTIAL_HELP = "run 'verdict setup --apply --allow host.serve' to create it"


#: Returned by the pre-dispatch checks when the attempt's own budget ran out first.
_BUDGET_EXHAUSTED = "budget exhausted"


def _probe_env(base_env: dict[str, str]) -> dict[str, str]:
    """The environment for the version probe: the worker allowlist, and no credentials."""
    return {
        key: value
        for key, value in base_env.items()
        if key in _CHILD_ENV_ALLOWLIST
        or any(key.startswith(prefix) for prefix in _CHILD_ENV_ALLOWLIST_PREFIXES)
    }


def _check_binary(
    binary: str, timeout: float = 15.0, env: dict[str, str] | None = None
) -> str | None:
    """None when ``binary --version`` runs; else a short, non-secret reason.

    The probe runs in its own process group with a credential-free environment, and the whole
    group is killed on every exit (a helper that keeps the pipes open must not hold it).
    """
    resolved = shutil.which(binary) if os.sep not in binary else binary
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        return f"opencode binary not found or not executable at {binary!r}"
    probe_env = _probe_env(dict(os.environ) if env is None else env)
    try:
        proc = subprocess.Popen(
            [resolved, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,  # only the exit status matters: no pipe a helper can hold
            stderr=subprocess.DEVNULL,
            env=probe_env,
            start_new_session=True,
        )
    except OSError as exc:
        return f"opencode binary is not runnable ({type(exc).__name__})"
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return (
            _BUDGET_EXHAUSTED if timeout < 15.0 else "opencode binary is not runnable (timed out)"
        )
    except OSError as exc:
        return f"opencode binary is not runnable ({type(exc).__name__})"
    finally:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    if proc.returncode != 0:
        return f"opencode binary is not runnable (`--version` exited {proc.returncode})"
    return None


def _check_serve(origin: str, token: str, timeout: float) -> str | None:
    """None when serve at ``origin`` is reachable and accepts ``token``; else a short reason.

    One authenticated ``GET /v1/guidance/status`` (read-only, answered without any upstream
    call).  No redirect is followed, so the credential cannot be sent elsewhere.  The reason
    never contains the credential.
    """
    from verdict.http_safety import open_no_redirect

    request = urllib.request.Request(
        f"{origin.rstrip('/')}/v1/guidance/status",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with open_no_redirect(request, timeout=timeout):
            return None
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return f"verdict serve at {origin} rejected the worker credential (HTTP {exc.code})"
        return f"verdict serve at {origin} answered HTTP {exc.code} to the readiness check"
    except (urllib.error.URLError, OSError, TimeoutError):
        return f"verdict serve is unreachable at {origin}"


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
    api_key_value: str | None = None,
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
    credential from reaching the worker.  The worker credential is set explicitly, only
    under ``api_key_env`` (so the ``{env:...}`` reference in the per-attempt config can
    expand at runtime), from ``api_key_value``; a value for that name already in
    ``base_env`` never reaches the child when one is passed.
    """
    source = dict(base_env if base_env is not None else os.environ)
    env: dict[str, str] = {}
    for key, val in source.items():
        if (
            (key == api_key_env and api_key_value is None)
            or key in _CHILD_ENV_ALLOWLIST
            or any(key.startswith(p) for p in _CHILD_ENV_ALLOWLIST_PREFIXES)
        ):
            env[key] = val
    if api_key_value is not None:
        env[api_key_env] = api_key_value
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

    An ``error`` event is ``{"error": {"message", "status", "retryAfterMs", ...}}``: the HTTP
    status and retry delay are kept so recovery can classify an upstream 429 or 503.

    Cost is recorded only when OpenCode reports a real positive figure.  A custom provider
    has no pricing, so OpenCode reports 0 even for a paid route; that is "unknown", not
    "free", so it is ``None`` with a ``cost_note``.  Tokens are still recorded.

    Returns a dict with keys: ``usage``, ``session_id``, ``last_text``,
    ``error_message``, ``error_status``, ``retry_after_seconds``.
    """
    session_id = ""
    text_parts: list[str] = []
    error_message = ""
    error_status: int | None = None
    retry_after_seconds: float | None = None

    total_input: int | None = None
    total_output: int | None = None
    total_cost: float | None = None
    all_have_cost = True
    saw_zero_cost = False
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
                if cost_val is not None and cost_val > 0:
                    total_cost = (total_cost or 0.0) + cost_val
                else:
                    # No figure, or a 0: a custom provider has no pricing, so a 0 is
                    # "unknown", not "free".  One such step makes the attempt's total unknown
                    # rather than an under-count.
                    all_have_cost = False
                    saw_zero_cost = saw_zero_cost or cost_val is not None

        elif event_type == "error":
            detail = event.get("error")
            msg = event.get("message")
            structured_status: int | None = None
            if isinstance(detail, dict):
                msg = detail.get("message") or msg
                structured_status = _as_int(detail.get("status"))
                error_status = structured_status
                retry_ms = _as_float(detail.get("retryAfterMs"))
                if retry_ms is not None and retry_ms > 0:
                    retry_after_seconds = retry_ms / 1000.0
            elif not msg and isinstance(detail, str):
                msg = detail
            if isinstance(msg, str) and msg:
                full_message = _sanitize_full(msg, secrets)
                error_message = full_message[-_STDERR_TAIL_CHARS:]
                # The status belongs to the event whose message is kept: its structured status,
                # else one read from its whole text.  The recorded text is only the tail, and a
                # long provider diagnostic after "HTTP 429" would otherwise cut the status away
                # before recovery could classify it.  An earlier event's status never carries
                # over to this message.
                error_status = (
                    structured_status
                    if structured_status is not None
                    else _status_from_full_text(full_message)
                )

    usage: AttemptUsage | None = None
    if has_usage:
        if all_have_cost and total_cost is not None and total_cost > 0:
            cost_usd: float | None = total_cost
            cost_note: str | None = None
        else:
            cost_usd = None
            cost_note = (
                "not_reported: opencode reported 0 for a step (no pricing for a custom provider)"
                if saw_zero_cost
                else "not_reported: opencode gave no cost"
            )
        usage = AttemptUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            cost_usd=cost_usd,
            tokens_source="opencode_ndjson",
            cost_note=cost_note,
        )

    return {
        "usage": usage,
        "session_id": _redact(session_id, secrets),
        "last_text": _redact("".join(text_parts), secrets),
        "error_message": error_message,
        "error_status": error_status,
        "retry_after_seconds": retry_after_seconds,
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


def _cleanup_attempt_dir(attempt_tmp: tempfile.TemporaryDirectory[str]) -> None:
    """Remove the per-attempt directory; never raise, and never leave it if it can be removed.

    This runs in a ``finally`` after the result is known, so an OSError from it (a busy or
    unreadable entry on a failing disk) must not replace that result with an exception.  When
    the first attempt fails, a forced removal is tried; the directory holds no credential (the
    key is only ever in the child environment), so a leftover is a disk-space problem, not a leak.
    """
    try:
        attempt_tmp.cleanup()
    except OSError:
        shutil.rmtree(attempt_tmp.name, ignore_errors=True)


class _WorkerIOError(OSError):
    """Reading the worker's pipes (or waiting on it) failed; the attempt cannot be trusted."""


#: Longest wait for the serve readiness check before an attempt is launched.
_SERVE_CHECK_TIMEOUT_SECONDS = 5.0


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
        serve_base_url: str | None = None,
        api_key_env: str = WORKER_API_KEY_ENV,
        idle_output_seconds: float | None = None,
        max_output_bytes: int | None = None,
        credential_provider: Callable[[str, str], str | None] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        from verdict.serve_service import resolve_serve_origin

        self._opencode_bin = opencode_bin
        # The one serve address: the configured origin, else the unit's default.
        self._serve_base_url = resolve_serve_origin(serve_base_url)
        self._api_key_env = api_key_env
        self._credential_provider = credential_provider
        self._binary_checked = False
        self._env = dict(env) if env is not None else None
        self.max_output_bytes: int = (
            max_output_bytes if max_output_bytes is not None else MAX_OUTPUT_BYTES
        )
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

    def _failure(self, route_id: str, started: float, error: str) -> WorkerTerminal:
        return WorkerTerminal(
            executor_kind="live",
            ok=False,
            model=route_id,
            harness="opencode",
            error=error,
            duration_seconds=time.monotonic() - started,
        )

    def _pre_dispatch_reason(self, credential: str, deadline: float) -> str | None:
        """Why this attempt cannot be launched (binary, then serve), or None.  Blocking.

        Both checks share the attempt's absolute ``deadline`` (``time.monotonic()``): each gets
        at most what is left, and running out is :data:`_BUDGET_EXHAUSTED`, not a block.
        """
        if not self._binary_checked:
            left = deadline - time.monotonic()
            if left <= 0:
                return _BUDGET_EXHAUSTED
            reason = _check_binary(self._resolve_bin(), min(15.0, left), self._base_env())
            if reason is not None:
                return reason
            self._binary_checked = True
        left = deadline - time.monotonic()
        if left <= 0:
            return _BUDGET_EXHAUSTED
        reason = _check_serve(
            self._serve_base_url, credential, min(_SERVE_CHECK_TIMEOUT_SECONDS, left)
        )
        if reason is not None and time.monotonic() >= deadline:
            return _BUDGET_EXHAUSTED  # the check ran into the attempt's own deadline
        return reason

    async def _run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        started = time.monotonic()
        session_id = _attempt_session_id()

        # --- pre-dispatch: credential, binary, serve (one message, nothing launched) ---
        provider = self._credential_provider or _worker_credential
        try:
            credential = provider(route_id, session_id)
        except Exception as exc:  # a malformed store or file must not escape as an exception
            return self._failure(
                route_id,
                started,
                f"harness_configuration: serve credential lookup failed ({type(exc).__name__}); "
                f"{_CREDENTIAL_HELP}",
            )
        if not credential:
            return self._failure(
                route_id,
                started,
                "harness_configuration: no verdict serve credential found (looked in the "
                f"environment, the Verdict credential store and serve.env); {_CREDENTIAL_HELP}",
            )
        secrets = [credential, self._base_env().get(self._api_key_env, "")]
        reason = await asyncio.to_thread(
            self._pre_dispatch_reason, credential, started + timeout_seconds
        )
        if reason == _BUDGET_EXHAUSTED:
            return self._failure(route_id, started, "timeout")
        if reason is not None:
            return self._failure(
                route_id, started, f"harness_configuration: {_sanitize(reason, secrets=secrets)}"
            )

        # --- write context file to worktree (Task 4.1) ---
        try:
            await _prepare_context(cwd, prompt, started + timeout_seconds)
        except _PreparationDeadlineError:
            return self._failure(route_id, started, "timeout")
        except OSError as exc:
            return self._failure(
                route_id, started, f"context_write_failed: {_sanitize(str(exc), secrets=secrets)}"
            )

        # Preparation (two git probes of up to 30 s each) counts against the attempt budget:
        # the worker gets only what is left, and nothing is spawned when nothing is.
        if timeout_seconds - (time.monotonic() - started) <= 0:
            return self._failure(route_id, started, "timeout")

        # --- create per-attempt temp directories (Task 2.3) ---
        try:
            attempt_tmp = tempfile.TemporaryDirectory(prefix="verdict-oc-")
        except OSError as exc:
            # A full or unwritable temp filesystem: ``run()`` returns a terminal, never raises.
            return self._failure(
                route_id,
                started,
                f"config_write_failed: cannot create the per-attempt directory: "
                f"{_sanitize(str(exc), secrets=secrets)}",
            )
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
                return self._failure(
                    route_id,
                    started,
                    f"config_write_failed: {_sanitize(str(exc), secrets=secrets)}",
                )

            child_env = _build_child_env(
                config_dir=config_dir,
                data_dir=data_dir,
                state_dir=state_dir,
                api_key_env=self._api_key_env,
                api_key_value=credential,
                base_env=self._base_env(),
            )

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
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=child_env,
                    start_new_session=True,
                )
            except OSError as exc:
                return self._failure(
                    route_id, started, f"spawn_failed: {_sanitize(str(exc), secrets=secrets)}"
                )

            try:
                try:
                    stdout_b, stderr_b, cut = await self._collect(
                        proc, max(0.0, timeout_seconds - (time.monotonic() - started))
                    )
                finally:
                    # Always reap the whole process group before returning, whatever the
                    # path (normal exit, stall, timeout, cancellation or any error): a
                    # descendant with redirected pipes can outlive its leader and keep
                    # editing the worktree while owned-files validation runs.
                    await _kill_process_group(proc)
            except _WorkerIOError as exc:
                return self._failure(
                    route_id, started, f"worker_io_failed: {_sanitize(str(exc), secrets=secrets)}"
                )
            except Exception as exc:
                return self._failure(route_id, started, f"worker_io_failed: {type(exc).__name__}")
            if cut:
                stalled = cut == "stalled"
                if stalled:
                    cut_error = f"stalled: no worker output for {self.idle_output_seconds:g}s"
                elif cut == "output_limit":
                    cut_error = (
                        f"output_limit: worker output exceeded {self.max_output_bytes} bytes"
                    )
                else:
                    cut_error = "timeout"
                return replace(
                    self._failure(route_id, started, cut_error),
                    stop_reason="stalled" if stalled else "",
                )

            return self._interpret(
                stdout=stdout_b.decode("utf-8", "replace"),
                stderr=stderr_b.decode("utf-8", "replace"),
                returncode=proc.returncode if proc.returncode is not None else -1,
                route_id=route_id,
                duration=time.monotonic() - started,
                session_id=session_id,
                secrets=secrets,
            )
        finally:
            _cleanup_attempt_dir(attempt_tmp)

    async def _collect(
        self, proc: asyncio.subprocess.Process, timeout_seconds: float
    ) -> tuple[bytes, bytes, str]:
        """Collect both pipes with timeout and idle-output stall kill.

        Returns ``(stdout, stderr, cut)``; ``cut`` is ``""`` on normal exit,
        ``"timeout"`` when the whole attempt runs past ``timeout_seconds``,
        ``"stalled"`` when no output byte arrived for ``idle_output_seconds``, and
        ``"output_limit"`` when stdout plus stderr exceed ``max_output_bytes``.  A pipe
        read that fails raises :class:`_WorkerIOError`: partial output is never
        presented as a finished attempt.
        """
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        last_output = time.monotonic()

        over_limit = asyncio.Event()

        async def pump(name: str, stream: asyncio.StreamReader | None) -> None:
            nonlocal last_output
            if stream is None:
                return
            while chunk := await stream.read(65536):
                buffers[name] += chunk
                last_output = time.monotonic()
                if len(buffers["stdout"]) + len(buffers["stderr"]) > self.max_output_bytes:
                    over_limit.set()
                    return

        tasks = {
            asyncio.ensure_future(pump("stdout", proc.stdout)),
            asyncio.ensure_future(pump("stderr", proc.stderr)),
            asyncio.ensure_future(proc.wait()),
        }
        # Wakes the loop as soon as the output cap is hit, instead of at the next timer.
        limit_waiter = asyncio.ensure_future(over_limit.wait())
        started = time.monotonic()
        cut = ""
        try:
            while True:
                if over_limit.is_set():
                    cut = "output_limit"
                    break
                if any(t.done() and not t.cancelled() and t.exception() is not None for t in tasks):
                    break  # a failed read or wait: stop now, report it below
                # Wait only on unfinished work: a task that is already done would make
                # FIRST_COMPLETED return at once, every time, and spin the shared event loop.
                unfinished = {task for task in tasks if not task.done()}
                if not unfinished:
                    break
                now = time.monotonic()
                hard_left = timeout_seconds - (now - started)
                idle_left = self.idle_output_seconds - (now - last_output)
                if hard_left <= 0:
                    cut = "timeout"
                    break
                if idle_left <= 0:
                    cut = "stalled"
                    break
                await asyncio.wait(
                    {*unfinished, limit_waiter},
                    timeout=min(hard_left, idle_left),
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            for task in (*tasks, limit_waiter):
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, limit_waiter, return_exceptions=True)
        # Reached only when nothing is propagating (a cancellation skips this): a pump or
        # wait that really failed (not one we cancelled ourselves) must not be discarded.
        for result in results:
            if isinstance(result, Exception):
                raise _WorkerIOError(f"{type(result).__name__}: {result}") from result
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
        secrets: Iterable[str] = (),
    ) -> WorkerTerminal:
        """Map opencode output to WorkerTerminal.

        Exit code is authoritative: 0 → ok=True, non-zero → ok=False.
        NDJSON is parsed for usage (``step_finish`` tokens), the session ID and, on a
        failure, the upstream HTTP status and retry delay it reports.
        """
        secrets = list(secrets)
        parsed = _parse_ndjson_output(stdout, secrets)
        usage = parsed["usage"]
        actual_session_id = parsed["session_id"] or session_id
        text = parsed["last_text"]
        error_message = parsed["error_message"]

        if returncode != 0:
            # The status is read from the whole sanitized text, then the tail is kept: cutting
            # first would lose a status that a long diagnostic pushed out of the last 400 chars.
            # An NDJSON error's status was already read (structured, else from its full text).
            stderr_full = "" if error_message else _sanitize_full(stderr, secrets)
            tail = error_message or stderr_full[-_STDERR_TAIL_CHARS:] or f"exit {returncode}"
            status = parsed["error_status"] or _status_from_full_text(stderr_full)
            return WorkerTerminal(
                executor_kind="live",
                ok=False,
                model=route_id,
                harness="opencode",
                error=_sanitize(tail, secrets=secrets),
                status_code=status,
                retry_after_seconds=parsed["retry_after_seconds"],
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
