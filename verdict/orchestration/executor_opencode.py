"""OpenCode worker executor for Verdict orchestration.

Runs one task on one exact OmniRoute route through OpenCode's non-interactive
dispatch (``opencode run --standalone --auto --format json --archived``).

Each attempt:

0. Checks the run id, the node id and the attempt timeout, signs a worker assignment for this
   one attempt (:func:`verdict.worker_assignment.mint_assignment`, key from
   :func:`~verdict.worker_assignment.load_assignment_key`) and checks, once and before anything
   is launched, that the opencode binary runs and that ``verdict serve`` is reachable and
   accepts the *serve* token.  Any failure is one ``harness_configuration:`` message, which
   recovery treats as a block, not a retry.  The assignment is the worker's only credential for
   serve: it names the route, the run and the node and expires with the attempt.  The serve token
   stays in this (the orchestrator's) process, for that readiness check alone; it is never put in
   the worker's environment, arguments, config or recorded output, and it is never the fallback
   when an assignment cannot be signed.
1. Writes a per-attempt ``opencode.json`` to a temp directory, pointed at by
   ``OPENCODE_CONFIG_DIR``, and the task text beside it as the instructions file
   :data:`TASK_FILE_NAME`, which that config lists.  OpenCode skips a listed file that is
   missing without a word, so the file is checked to exist, to be a readable file and not to
   be empty just before launch, and an attempt without it never starts.
2. Launches the opencode binary, hands it the one short ask :data:`TASK_PROMPT` on stdin (it
   reads its message from stdin when it is given none) and ``--title`` (so it makes no second,
   title-agent request), and collects NDJSON stdout.  The task text reaches the model once, as
   instructions; nothing is written into the worktree.
3. Maps the result to :class:`~verdict.orchestration.contracts.WorkerTerminal`.
4. Deletes the temp directory, and with it the instructions file, on every way out.
5. Reads back what serve recorded under the assignment (its digest, never the credential) and puts
   it on the terminal, so the run receipt shows which attempts ran with a requirement waived
   (:mod:`verdict.orchestration.serve_evidence`).

**Dispatch origin** — OpenCode workers send through Verdict serve (:8000), not the gateway
(OmniRoute :20128), so this executor reports the serve origin it really calls (the address it
resolved, as a :class:`~verdict.orchestration.contracts.WorkerServeOrigin`) and the runtime
checks it against ``DispatchAuthority.worker_serve_origin`` before any send (#1326).  It never
claims to send nothing: it sends.
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
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from verdict.orchestration.contracts import AttemptUsage, WorkerServeOrigin, WorkerTerminal
from verdict.orchestration.executors import idle_output_seconds_from_env
from verdict.orchestration.serve_evidence import EvidenceMark, read_assignment_evidence, take_mark
from verdict.worker_assignment import (
    MAX_LIFETIME_SECONDS,
    AssignmentError,
    assignment_digest,
    load_assignment_key,
    mint_assignment,
)

__all__ = ["PINNED_VERSION", "OpenCodeExecutor"]

#: Pinned OpenCode version for this adapter.  ``verdict doctor`` warns on mismatch.
PINNED_VERSION = "2.3.2"
#: Env var to override the opencode binary path.
OPENCODE_BIN_ENV = "VERDICT_OPENCODE_BIN"
#: Env var the per-attempt config references for the worker API key.
WORKER_API_KEY_ENV = "VERDICT_WORKER_API_KEY"
#: The instructions file that carries the task text, written beside ``opencode.json`` in the
#: per-attempt config directory (never in the worktree) and listed in that config's
#: ``instructions``.
TASK_FILE_NAME = "verdict-task.md"
#: The whole prompt of an attempt, sent on stdin: one short ask that points at the instructions.
#: It is the same for every task; the task text itself is only in :data:`TASK_FILE_NAME`, so no
#: text is sent twice and the prompt never nears the 131072-byte limit of one argument.
TASK_PROMPT = (
    "Carry out the task described in your instructions (the Verdict task file). "
    "Make the edits it asks for, run its verification, and stop when verification passes."
)

#: Seconds a signed assignment stays valid past the attempt's own timeout.  The assignment is
#: dated just before the pre-dispatch checks (which spend the attempt's budget), so its end falls
#: this far after the attempt's deadline: room for the worker's last request, the kill grace
#: (:data:`_TERM_GRACE_SECONDS`) and clock rounding, and nothing more.  It is not a retry window:
#: an attempt whose timeout plus this margin would exceed the format's maximum lifetime
#: (:data:`~verdict.worker_assignment.MAX_LIFETIME_SECONDS`, 1,800 s) is refused before launch,
#: never shortened.
ASSIGNMENT_MARGIN_SECONDS = 60

_TERM_GRACE_SECONDS = 5.0
#: Cap on worker output (stdout plus stderr) kept in memory per attempt.  NDJSON is parsed
#: after the run and several attempts run concurrently, so a worker that prints without
#: end must fail its attempt rather than exhaust the orchestrator.
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_STDERR_TAIL_CHARS = 400
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: Reason phrases that may follow a bare ``429`` as in ``429 Too Many Requests``.  A fixed list
#: (not ``http.HTTPStatus``, whose wording differs between Python versions).
_STATUS_PHRASES = (
    "Bad Request",
    "Unauthorized",
    "Payment Required",
    "Forbidden",
    "Not Found",
    "Method Not Allowed",
    "Request Timeout",
    "Conflict",
    "Payload Too Large",
    "Unprocessable Entity",
    "Too Many Requests",
    "Internal Server Error",
    "Not Implemented",
    "Bad Gateway",
    "Service Unavailable",
    "Gateway Timeout",
)
#: An HTTP status read from unstructured text: only a 4xx or 5xx number that is marked as one.
#: Either it follows ``HTTP`` (``HTTP 429``, ``HTTP/1.1 503``, ``HTTP error 500``) or ``status``
#: (``status 503``, ``status: 504``, ``status code: 401``, ``statusCode=403``, ``"status":429``),
#: or it is followed by its standard reason phrase (``429 Too Many Requests``), or by the wording
#: of the ``requests`` library's ``HTTPError`` (``429 Client Error: Too Many Requests for url:
#: ...``, ``503 Server Error: Service Unavailable for url: ...``: ``Client`` for a 4xx, ``Server``
#: for a 5xx, then a colon).  A bare number is not a status: ``retrying in 500 ms`` and
#: ``failed after 429 attempts`` carry none.
_STATUS_RE = re.compile(
    r"""(?ix)
    (?: \bhttp(?:/\d(?:\.\d)?)? (?:\s+(?:status|error|code|response))*
      | \bstatus(?:[\s_-]?code)? )
    ["']? (?: \s*[:=]\s* | \s+ ) ["']?
    ([45]\d\d)\b
    |
    \b([45]\d\d)\b (?= \s+ (?: """
    + "|".join(r"\s+".join(map(re.escape, phrase.split())) for phrase in _STATUS_PHRASES)
    + r""" ) \b )
    |
    \b(4\d\d)\b (?= \s+ Client \s+ Error \s* : )
    |
    \b(5\d\d)\b (?= \s+ Server \s+ Error \s* : )
    """
)


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
    """The HTTP status that ``message`` states with an HTTP or status marker, or None.

    Only for unstructured text: an explicit ``status`` field of a structured error never comes
    through here.  A number that merely sits in the text (a delay, a count) is not a status.
    """
    match = _STATUS_RE.search(message)
    return int(next(group for group in match.groups() if group)) if match else None


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


#: The one message ``verdict serve`` answers any refused worker assignment with (HTTP 403, body
#: ``{"error": {"message": "authentication failed", ...}}``): expired, tampered, signed with
#: another key or dated ahead all look alike on purpose.
_SERVE_REFUSAL_MESSAGE = "authentication failed"


def _is_serve_refusal(status: int | None, message: str) -> bool:
    """True when a failed worker's error is serve refusing the worker's own credential.

    Recovery would otherwise read the 401/403 as the route's provider denying the account and
    cool the provider down for an hour, for a fault that is Verdict's own (a signing key serve
    does not share, a clock that disagrees, an assignment that expired under a slow attempt).
    """
    return status in {401, 403} and message.strip().rstrip(".").lower() == _SERVE_REFUSAL_MESSAGE


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

    ``instructions`` lists the task file :data:`TASK_FILE_NAME` in ``config_dir`` itself (an
    absolute path; :func:`_write_task_file` writes it, and :func:`_task_file_problem` checks it
    is there before launch, since OpenCode skips a listed file that is missing).  When an
    ``AGENTS.md`` file is present in ``cwd`` it is also listed, so the worker sees project
    coding conventions.

    No worker header is sent: ``verdict serve`` selects nothing from ``X-Verdict-Worker``.  The
    route is pinned by the signed assignment the worker presents as its bearer (the value of
    ``api_key_env``), which names the route, the run and the node.

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
    instructions: list[str] = [str(config_dir / TASK_FILE_NAME)]
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


def _write_task_file(config_dir: Path, task: str) -> None:
    """Write the task text to ``config_dir / TASK_FILE_NAME``, the instructions file.

    ``config_dir`` lives in the attempt's own private temp directory, never in the worktree, so
    nothing here follows a path the repository controls and nothing is left behind in it.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / TASK_FILE_NAME).write_text("# Task\n\n" + task + "\n", encoding="utf-8")


def _task_file_problem(config_dir: Path) -> str | None:
    """Why the instructions file cannot be relied on, or None when it is there and readable.

    OpenCode skips a listed instructions file that does not exist and carries on, so a worker
    would run without its task and its output would look like a failed attempt.  The adapter
    asks first: the file must be a regular file (a directory is not), must open and must not be
    empty.  The reason names the file, never the task text.
    """
    path = config_dir / TASK_FILE_NAME
    try:
        if not path.is_file():
            return f"the task instructions file {TASK_FILE_NAME} is missing"
        with path.open("rb") as handle:
            if not handle.read(1):
                return f"the task instructions file {TASK_FILE_NAME} is empty"
    except OSError as exc:
        return f"the task instructions file {TASK_FILE_NAME} cannot be read ({type(exc).__name__})"
    return None


_T = TypeVar("_T")


async def _to_thread_joined(
    func: Callable[..., _T], *args: Any, on_cancel: Callable[[], None] | None = None
) -> _T:
    """Run ``func(*args)`` in a thread; a cancellation waits for the thread before it propagates.

    A thread cannot be interrupted, and cancelling the await alone leaves it running.  So if
    this coroutine is cancelled, ``on_cancel`` runs first (it asks the thread to stop early, for
    example by setting an event the thread polls), then the thread is waited for however many
    more times the cancellation repeats.  Whatever the thread does, it has finished by the time
    the caller sees ``CancelledError``: a caller that has seen the cancellation complete can
    clean up or reuse what the thread touched without a late write, process or request racing it.
    """
    task = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # cancelled again: still wait for the thread
            except Exception:
                break  # the thread's work failed; the cancellation still wins
        if task.done() and not task.cancelled():
            task.exception()  # mark retrieved: the cancellation is what propagates
        raise


async def _send_prompt(proc: asyncio.subprocess.Process, prompt: str) -> None:
    """Write ``prompt`` to the worker's stdin and close it (OpenCode's message, when given none).

    A worker that has already exited (or closed its stdin) makes the write a broken pipe.  That
    is not an error of this call: how the worker ended is read from its exit and its output.
    The ask is a few hundred bytes, far below a pipe's capacity, so the write never blocks.
    """
    stdin = proc.stdin
    if stdin is None:
        return
    try:
        stdin.write(prompt.encode("utf-8"))
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(OSError):
            stdin.close()


def _serve_token() -> str | None:
    """Serve's own bearer token, for this process's readiness check of serve and nothing else.

    Resolved by Verdict's code (the same sources and order ``verdict setup`` uses: environment,
    credential store, ``serve.env``).  It is never handed to a worker: the worker's credential is
    the signed assignment of :meth:`OpenCodeExecutor._sign`.  Never the OmniRoute gateway key.
    """
    from verdict.serve_service import resolve_serve_token

    resolved = resolve_serve_token()
    return resolved[0] if resolved else None


def _assignment_lifetime(timeout_seconds: float) -> int:
    """Seconds the attempt's assignment lives: its timeout, rounded up, plus the margin.

    ``ValueError`` (with a reason that names no secret) when the timeout is not a positive, finite
    number, or when the lifetime would exceed the 1,800 s the format allows.  It is never clamped:
    a shorter assignment than the attempt would expire under a running worker.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("the attempt timeout is not a positive, finite number of seconds")
    lifetime = math.ceil(timeout_seconds) + ASSIGNMENT_MARGIN_SECONDS
    if lifetime > MAX_LIFETIME_SECONDS:
        raise ValueError(
            f"an attempt timeout of {timeout_seconds:g}s needs an assignment lifetime of "
            f"{lifetime}s ({ASSIGNMENT_MARGIN_SECONDS}s margin included), over the "
            f"{MAX_LIFETIME_SECONDS}s maximum; lower the attempt timeout"
        )
    return lifetime


_CREDENTIAL_HELP = "run 'verdict setup --apply --allow host.serve' to create it"


#: Returned by the pre-dispatch checks when the attempt's own budget ran out first.
_BUDGET_EXHAUSTED = "budget exhausted"
#: Returned by the pre-dispatch checks when the attempt was cancelled while they ran.  The
#: caller is being cancelled, so nobody reads it; it only ends the checks without a request.
_CANCELLED = "cancelled"
#: How often the version probe looks at the cancellation event while it waits for the process.
_PROBE_POLL_SECONDS = 0.05


def _probe_env(base_env: dict[str, str]) -> dict[str, str]:
    """The environment for the version probe: the worker allowlist, and no credentials."""
    return {
        key: value
        for key, value in base_env.items()
        if key in _CHILD_ENV_ALLOWLIST
        or any(key.startswith(prefix) for prefix in _CHILD_ENV_ALLOWLIST_PREFIXES)
    }


class _ProbeCancelledError(Exception):
    """The attempt was cancelled while the version probe was running."""


def _wait_for_probe(
    proc: subprocess.Popen[bytes], timeout: float, cancel: threading.Event | None
) -> None:
    """Wait for ``proc`` to exit, for at most ``timeout`` seconds, watching ``cancel``.

    Raises :class:`subprocess.TimeoutExpired` when the time is up and
    :class:`_ProbeCancelledError` when ``cancel`` is set first.  The process is left to the
    caller to kill.
    """
    if cancel is None:
        proc.wait(timeout=timeout)
        return
    end = time.monotonic() + timeout
    while True:
        if cancel.is_set():
            raise _ProbeCancelledError
        left = end - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            proc.wait(timeout=min(_PROBE_POLL_SECONDS, left))
            return
        except subprocess.TimeoutExpired:
            continue


def _check_binary(
    binary: str,
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
) -> str | None:
    """None when ``binary --version`` runs; else a short, non-secret reason.

    The probe runs in its own process group with a credential-free environment, and the whole
    group is killed on every exit (a helper that keeps the pipes open must not hold it).  When
    ``cancel`` is set the wait ends within :data:`_PROBE_POLL_SECONDS`, the group is killed and
    reaped, and :data:`_CANCELLED` is returned.
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
        _wait_for_probe(proc, timeout, cancel)
    except _ProbeCancelledError:
        return _CANCELLED
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
    except ValueError:
        # http.client refuses a header value it cannot carry (an embedded newline, a character
        # outside latin-1); the reason never repeats the credential.
        return "the verdict serve credential is not a valid HTTP header value"


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

    OpenCode emits one JSON object per line.  The shape is the one OpenCode 2.3.2 really prints
    (captured in ``tests/fixtures/opencode_stdout``): ``type`` and ``sessionID`` are top-level
    and everything else about a model step sits under ``part``.  Interesting event types:

    - ``text`` — one whole text block in ``part.text`` (not an increment: a reply streamed in
      many deltas arrives as one event).  A run with tool calls prints one per block, and they
      are joined in order.
    - ``step_finish`` — end of one model step; ``part.tokens`` (``input``, ``output``) and
      ``part.cost`` are that step's own, and ``part.reason`` is its stop reason (``stop`` for
      the last step, ``tool-calls`` for one that ended in a tool call).
    - ``error`` — the final failure, ``{"error": {"message", "status", "retryAfterMs", ...}}``;
      non-zero exit always takes precedence.  OpenCode retries an upstream failure by itself
      and prints a ``retry`` event for each try before it; those are not errors of the attempt
      and are not read.

    Every string taken from the stream (error text, assistant text, session id, stop reason)
    has ``secrets`` and ``Authorization: Bearer`` values redacted after the JSON is decoded, so
    JSON escaping cannot hide a secret from the replacement and a secret split across ``text``
    blocks is caught once the blocks are joined.

    The final error's HTTP status and retry delay are kept, so recovery can classify an
    upstream 429 or 503.

    Cost is recorded only when OpenCode reports a real positive figure.  A custom provider
    has no pricing, so OpenCode reports 0 even for a paid route; that is "unknown", not
    "free", so it is ``None`` with a ``cost_note``.  Tokens are still recorded.

    Returns a dict with keys: ``usage``, ``session_id``, ``last_text``, ``stop_reason`` (the
    last step's, or ``None``), ``error_message``, ``error_status``, ``retry_after_seconds``.
    """
    session_id = ""
    text_parts: list[str] = []
    stop_reason: str | None = None
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
        except (ValueError, RecursionError):
            # Not JSON, or nested so deeply that the decoder gives up (RecursionError, not
            # ValueError): a worker can print such a line within the output cap.  Skip it.
            continue
        if not isinstance(event, dict):
            continue

        sid = event.get("sessionID", "")
        if isinstance(sid, str) and sid and not session_id:
            session_id = sid

        event_type = event.get("type", "")

        part = event.get("part")
        if not isinstance(part, dict):
            part = {}

        if event_type == "text":
            block = part.get("text", "")
            if isinstance(block, str):
                text_parts.append(block)

        elif event_type == "step_finish":
            reason = part.get("reason")
            if isinstance(reason, str) and reason:
                stop_reason = reason
            tokens = part.get("tokens") or {}
            if isinstance(tokens, dict):
                inp = _as_int(tokens.get("input"))
                out = _as_int(tokens.get("output"))
                cost_raw = part.get("cost")
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
            event_retry: float | None = None
            if isinstance(detail, dict):
                msg = detail.get("message") or msg
                structured_status = _as_int(detail.get("status"))
                retry_ms = _as_float(detail.get("retryAfterMs"))
                if retry_ms is not None and retry_ms > 0:
                    event_retry = retry_ms / 1000.0
            elif not msg and isinstance(detail, str):
                msg = detail
            full_message = _sanitize_full(msg, secrets) if isinstance(msg, str) else ""
            if full_message or structured_status is not None or event_retry is not None:
                # The final error event that says anything decides message, status and retry
                # delay together, so recovery never sees a status or a cooldown that belongs to
                # a different event than the text.  An event with nothing to say (no usable
                # message, status or delay) changes nothing.  The recorded text is only the
                # tail, and a long provider diagnostic after "HTTP 429" would otherwise cut the
                # status away before recovery could classify it.
                error_message = full_message[-_STDERR_TAIL_CHARS:]
                error_status = (
                    structured_status
                    if structured_status is not None
                    else _status_from_full_text(full_message)
                )
                retry_after_seconds = event_retry

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
        "stop_reason": _redact(stop_reason, secrets) if stop_reason is not None else None,
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
    the first attempt fails, a forced removal is tried.  The directory holds no credential (the
    key is only ever in the child environment) but it does hold the task text, in the instructions
    file, so it is removed on every way out of an attempt and a leftover is worth reporting.
    """
    try:
        attempt_tmp.cleanup()
    except OSError:
        shutil.rmtree(attempt_tmp.name, ignore_errors=True)


class _UnsignableError(Exception):
    """No worker assignment can be signed for this attempt; the message is safe to record."""


class _WorkerIOError(OSError):
    """Reading the worker's pipes (or waiting on it) failed; the attempt cannot be trusted."""


#: Longest wait for the serve readiness check before an attempt is launched.
_SERVE_CHECK_TIMEOUT_SECONDS = 5.0


@dataclass
class _Attempt:
    """What one attempt learned that :meth:`OpenCodeExecutor.run` needs once ``_run`` is done."""

    digest: str = ""  # SHA-256 of the assignment's payload; empty until one is signed
    mark: EvidenceMark | None = None  # serve's receipt database before the worker started
    launched: bool = False  # a worker process was started, so serve may hold requests for it


class OpenCodeExecutor:
    """Run one task through OpenCode's non-interactive dispatch.

    See the module docstring for the dispatch origin, the per-attempt isolation strategy and the
    signed assignment that is the worker's only credential.  ``serve_token_provider`` (the token
    for the readiness check), ``key_provider`` (the signing key), ``clock`` and
    ``receipts_db_provider`` (serve's receipt database) default to Verdict's own resolvers and
    exist so tests need no real home, key or database.
    """

    @property
    def gateway_origin(self) -> WorkerServeOrigin:
        """The serve origin this executor's workers call, reported to the dispatch check."""
        return WorkerServeOrigin(self._serve_base_url)

    def __init__(
        self,
        *,
        opencode_bin: str = "",
        serve_base_url: str | None = None,
        api_key_env: str = WORKER_API_KEY_ENV,
        idle_output_seconds: float | None = None,
        max_output_bytes: int | None = None,
        serve_token_provider: Callable[[], str | None] | None = None,
        key_provider: Callable[[], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        receipts_db_provider: Callable[[], Path | str | None] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        from verdict.serve_service import resolve_serve_origin

        self._opencode_bin = opencode_bin
        # The one serve address: the configured origin, else the unit's default.
        self._serve_base_url = resolve_serve_origin(serve_base_url)
        self._api_key_env = api_key_env
        self._serve_token_provider = serve_token_provider
        self._key_provider = key_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._receipts_db_provider = receipts_db_provider
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

    def _receipts_db(self) -> Path | str | None:
        """Serve's receipt database: the injected provider, else the environment or serve.env."""
        if self._receipts_db_provider is not None:
            return self._receipts_db_provider()
        from verdict.serve_service import resolve_serve_receipts_db

        return resolve_serve_receipts_db()

    async def run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> WorkerTerminal:
        """Run one task.  Never raises; always returns a terminal.

        ``run_id`` and ``node_id`` (the runtime passes both) are what the worker's signed
        assignment is bound to; without them no assignment is signed and nothing is launched.

        Cancellation still propagates.  Any other exception from the attempt is one failed
        terminal naming only the exception type: the message of an unforeseen error may carry
        worker output or a credential.  ``_run`` has already reaped the worker's process group
        and removed its directory by the time one reaches here.
        """
        started = time.monotonic()
        attempt = _Attempt()
        try:
            terminal = await self._run(
                prompt,
                route_id=route_id,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                node_id=node_id,
                attempt=attempt,
            )
        except Exception as exc:
            terminal = self._failure(route_id, started, f"worker_error: {type(exc).__name__}")
        if not terminal.harness:
            terminal = replace(terminal, harness="opencode")
        if attempt.launched and attempt.digest and attempt.mark is not None:
            record = await self._serve_evidence(attempt.mark, attempt.digest)
            terminal = replace(terminal, worker_assignment=record)
        return terminal

    async def _serve_evidence(self, mark: EvidenceMark, digest: str) -> dict[str, Any]:
        """What serve recorded under this attempt's assignment, once its worker is gone.

        Read in a thread (it opens a file) and never raises: any failure is an ``unavailable``
        record with its reason, which the run receipt shows as such.
        """
        try:
            serve_receipts = await _to_thread_joined(read_assignment_evidence, mark, digest)
        except Exception as exc:  # the reader does not raise; this is the last line
            serve_receipts = {
                "status": "unavailable",
                "reason": f"reading serve's receipts failed ({type(exc).__name__})",
                "requests": None,
                "requests_waived": None,
                "requirement_waivers": None,
            }
        return {"assignment_digest": digest, "serve_receipts": serve_receipts}

    def _failure(self, route_id: str, started: float, error: str) -> WorkerTerminal:
        return WorkerTerminal(
            executor_kind="live",
            ok=False,
            model=route_id,
            harness="opencode",
            error=error,
            duration_seconds=time.monotonic() - started,
        )

    def _pre_dispatch_reason(
        self, serve_token: str, deadline: float, cancel: threading.Event | None = None
    ) -> str | None:
        """Why this attempt cannot be launched (binary, then serve), or None.  Blocking.

        ``serve_token`` is serve's own token and is used here only, for the one readiness
        request; the worker never holds it.

        Both checks share the attempt's absolute ``deadline`` (``time.monotonic()``): each gets
        at most what is left, and running out is :data:`_BUDGET_EXHAUSTED`, not a block.  Once
        ``cancel`` is set the probe is killed, no further check starts (so no authenticated
        serve request is made for an attempt that has ended) and :data:`_CANCELLED` is returned.
        """
        if cancel is not None and cancel.is_set():
            return _CANCELLED
        if not self._binary_checked:
            left = deadline - time.monotonic()
            if left <= 0:
                return _BUDGET_EXHAUSTED
            reason = _check_binary(
                self._resolve_bin(), min(15.0, left), self._base_env(), cancel=cancel
            )
            if reason is not None:
                return reason
            self._binary_checked = True
        if cancel is not None and cancel.is_set():
            return _CANCELLED
        left = deadline - time.monotonic()
        if left <= 0:
            return _BUDGET_EXHAUSTED
        reason = _check_serve(
            self._serve_base_url, serve_token, min(_SERVE_CHECK_TIMEOUT_SECONDS, left)
        )
        if reason is not None and time.monotonic() >= deadline:
            return _BUDGET_EXHAUSTED  # the check ran into the attempt's own deadline
        return reason

    def _sign(
        self, route_id: str, run_id: str | None, node_id: str | None, timeout_seconds: float
    ) -> str:
        """Sign this attempt's assignment.  Blocking (it reads the key file).

        Raises :class:`_UnsignableError` with a message that names the problem and never the key,
        the token or an id.  Nothing falls back to the serve token: an attempt that cannot sign
        is not launched.
        """
        if not isinstance(run_id, str) or not run_id:
            raise _UnsignableError(
                "no run id was given for this attempt, so no worker assignment can be signed"
            )
        if not isinstance(node_id, str) or not node_id:
            raise _UnsignableError(
                "no node id was given for this attempt, so no worker assignment can be signed "
                "(a planner call has none)"
            )
        try:
            lifetime = _assignment_lifetime(timeout_seconds)
        except ValueError as exc:
            raise _UnsignableError(str(exc)) from exc
        try:
            key = (self._key_provider or load_assignment_key)()
        except AssignmentError as exc:
            raise _UnsignableError(
                f"the worker assignment signing key is unavailable ({exc.reason}); "
                f"{_CREDENTIAL_HELP}"
            ) from exc
        except Exception as exc:  # a custom provider; never let its message through
            raise _UnsignableError(
                f"the worker assignment signing key cannot be loaded ({type(exc).__name__}); "
                f"{_CREDENTIAL_HELP}"
            ) from exc
        try:
            return mint_assignment(
                key,
                route_id=route_id,
                run_id=run_id,
                node_id=node_id,
                lifetime_seconds=lifetime,
                now=self._clock(),
            )
        except ValueError as exc:
            raise _UnsignableError(f"the worker assignment cannot be signed: {exc}") from exc

    async def _run(
        self,
        prompt: str,
        *,
        route_id: str,
        cwd: Path,
        timeout_seconds: float,
        run_id: str | None,
        node_id: str | None,
        attempt: _Attempt,
    ) -> WorkerTerminal:
        started = time.monotonic()
        session_id = _attempt_session_id()

        # --- pre-dispatch: serve token, signed assignment, binary, serve (one message, nothing
        # launched).  The serve token is only for the readiness check below; the worker is given
        # the assignment alone. ---
        token_source = self._serve_token_provider or _serve_token
        try:
            serve_token = token_source()
        except Exception as exc:  # a malformed store or file must not escape as an exception
            return self._failure(
                route_id,
                started,
                f"harness_configuration: serve credential lookup failed ({type(exc).__name__}); "
                f"{_CREDENTIAL_HELP}",
            )
        if not serve_token:
            return self._failure(
                route_id,
                started,
                "harness_configuration: no verdict serve credential found (looked in the "
                f"environment, the Verdict credential store and serve.env); {_CREDENTIAL_HELP}",
            )
        if not serve_token.isprintable():
            # A control character (a newline from a mis-edited env file, say) can never be a
            # bearer token and cannot be sent in a header.  Name the problem, not the value.
            return self._failure(
                route_id,
                started,
                "harness_configuration: the verdict serve credential contains a control "
                f"character; {_CREDENTIAL_HELP}",
            )
        try:
            assignment = await _to_thread_joined(
                self._sign, route_id, run_id, node_id, timeout_seconds
            )
        except _UnsignableError as exc:
            return self._failure(route_id, started, f"harness_configuration: {exc}")
        attempt.digest = assignment_digest(assignment)
        _, payload_text, tag_text = assignment.split(".")
        secrets = [
            serve_token,
            assignment,
            tag_text,
            payload_text,
            self._base_env().get(self._api_key_env, ""),
        ]
        # The checks run in a thread (the probe and the request block).  On cancellation the
        # thread is told to stop and is waited for, so no probe process or serve request
        # outlives the attempt.
        cancel = threading.Event()
        reason = await _to_thread_joined(
            self._pre_dispatch_reason,
            serve_token,
            started + timeout_seconds,
            cancel,
            on_cancel=cancel.set,
        )
        if reason == _BUDGET_EXHAUSTED:
            return self._failure(route_id, started, "timeout")
        if reason is not None:
            return self._failure(
                route_id, started, f"harness_configuration: {_sanitize(reason, secrets=secrets)}"
            )

        # The pre-dispatch checks count against the attempt budget: the worker gets only what
        # is left, and nothing is spawned when nothing is.
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

            # The task goes in the instructions file beside the config, once; the worktree is
            # never written to.  A task that cannot be encoded fails closed like a full disk.
            try:
                _write_attempt_config(
                    config_dir,
                    route_id=route_id,
                    serve_base_url=self._serve_base_url,
                    api_key_env=self._api_key_env,
                    cwd=cwd,
                )
                _write_task_file(config_dir, prompt)
            except (OSError, UnicodeError) as exc:
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
                api_key_value=assignment,
                base_env=self._base_env(),
            )

            # The last thing before launch: OpenCode skips a listed instructions file that is not
            # there without a word, so a worker would run without its task.
            problem = _task_file_problem(config_dir)
            if problem is not None:
                return self._failure(route_id, started, f"config_write_failed: {problem}")

            # Where serve's receipt database stands before the worker can make a request: only
            # later rows can be this attempt's (read-only; a database that cannot be read is a
            # reason the run receipt shows, not a reason to stop the attempt).
            attempt.mark = await _to_thread_joined(take_mark, self._receipts_db())

            # ``--model verdict/<route_id>`` selects provider "verdict" and the
            # exact route registered in the per-attempt config (Task 2.2/2.3).  The message
            # is not an argument: with none, OpenCode reads it from stdin (:func:`_send_prompt`),
            # and ``--title`` stops it making a second, title-agent request on the route.
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
                "--title",
                f"verdict {session_id}",
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
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

            attempt.launched = True
            try:
                try:
                    await _send_prompt(proc, TASK_PROMPT)
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
            # Stderr is sanitized in every case.  An NDJSON error's own status (structured, else
            # from its full text) wins and its message is the recorded error; stderr only
            # supplies the status when that error carries none, so an ``HTTP 429`` printed
            # there still reaches recovery.
            stderr_full = _sanitize_full(stderr, secrets)
            tail = error_message or stderr_full[-_STDERR_TAIL_CHARS:] or f"exit {returncode}"
            status = parsed["error_status"] or _status_from_full_text(stderr_full)
            if _is_serve_refusal(status, error_message):
                return WorkerTerminal(
                    executor_kind="live",
                    ok=False,
                    model=route_id,
                    harness="opencode",
                    error=(
                        f"harness_configuration: verdict serve refused the worker assignment "
                        f"(HTTP {status}); an assignment is refused when it is expired, "
                        f"tampered, or signed with a key serve does not hold, so check that the "
                        f"orchestrator and serve share the signing key ({_CREDENTIAL_HELP}) and "
                        f"the clock"
                    ),
                    status_code=status,
                    duration_seconds=duration,
                    session_ref=actual_session_id,
                    usage=usage,
                )
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
