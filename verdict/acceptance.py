"""Scheduled live acceptance job: the A-table commands, run from the installed Verdict.

``verdict acceptance run`` runs each acceptance command as a subprocess of this
interpreter, so the hourly unit tests exactly what is installed. It writes
``$VERDICT_HOME/acceptance/<ts>.json`` (pass/fail per check, output digests),
keeps the newest results, and notifies the operator once when a green job
turns red for two runs, and once when it recovers.

The job can never be green while no FREE tool-capable route is eligible.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from verdict.acceptance_state import ISSUE_PREFIX, acceptance_dir, prune, update_alert_state
from verdict.http_safety import open_no_redirect
from verdict.security import redact_text

KEEP_RESULTS = 48
#: Exit status of a run that completed and recorded a red result. The unit
#: treats it as success, so a red result never also shows as a failed unit.
RED_EXIT = 3
NOTIFY_MODES = ("off", "dry-run", "send")
ROUTE_TASK = "Write a Python function that returns the sum of two integers."
_ELIGIBLE_STAGES = {"TASK_ELIGIBLE", "SELECTED"}


class AcceptanceBusyError(RuntimeError):
    """Another run (the timer or a manual run) holds the lock."""


@dataclass
class Check:
    name: str
    argv: list[str]
    passed: bool
    reason: str
    exit_code: int | None = None
    seconds: float = 0.0
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _tail(text: str, limit: int = 300) -> str:
    return redact_text(text.strip()[-limit:])


def run_cli(
    args: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    """``python -m verdict <args>`` with this interpreter; 124 on timeout."""
    try:
        done = subprocess.run(
            [sys.executable, "-m", "verdict", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return 124, out, f"timed out after {timeout:g}s"
    return done.returncode, done.stdout, done.stderr


Judge = Callable[[int, str, str], tuple[bool, str, dict[str, Any]]]


def gateway_env(gateway: str) -> dict[str, str]:
    """Every check runs against the one gateway this job measures."""
    return {**os.environ, "OMNIROUTE_BASE_URL": gateway}


def _cli_check(name: str, args: list[str], timeout: float, judge: Judge, gateway: str) -> Check:
    started = time.monotonic()
    code, out, err = run_cli(args, timeout, gateway_env(gateway))
    passed, reason, detail = judge(code, out, err)
    if not passed and err.strip():
        detail.setdefault("stderr_tail", _tail(err))
    return Check(
        name,
        ["verdict", *args],
        passed,
        reason,
        exit_code=code,
        seconds=round(time.monotonic() - started, 1),
        stdout_sha256=_digest(out),
        stderr_sha256=_digest(err),
        detail=detail,
    )


def _json(out: str) -> Any:
    try:
        return json.loads(out)
    except ValueError:
        return None


def judge_doctor(code: int, out: str, err: str) -> tuple[bool, str, dict[str, Any]]:
    """Doctor passes when it has no issue other than this job's own result.

    Contract examples:
    - Well-formed clean: `{"status": "ok", "issues": []}` with exit 0 -> PASS
    - Well-formed with only acceptance warning:
      `{"status": "failed", "issues": ["Live acceptance is red (x.json): ..."]}`
      with exit 0 or 1 -> PASS (deliberate allowance)
    - Well-formed with other issues:
      `{"status": "failed", "issues": ["gateway not reachable"]}` -> FAIL
    - Crashed or error payload:
      `{"error": "doctor crashed"}` -> FAIL (exit != 0 or error field)
    - Malformed schema missing issues list:
      `{"status": "failed"}` -> FAIL
    - Nonzero exit with empty issues:
      `{"issues": []}` with exit 1 -> FAIL (cannot pass implicitly)
    """
    payload = _json(out)
    if not isinstance(payload, dict):
        first_err = redact_text(err.strip().splitlines()[-1] if err.strip() else f"exit {code}")[
            :200
        ]
        return (
            False,
            f"doctor printed no JSON (exit {code})",
            {"exit_code": code, "error": first_err},
        )

    if payload.get("error"):
        err_msg = redact_text(str(payload["error"]))[:200]
        return (
            False,
            f"doctor error (exit {code}): {err_msg[:160]}",
            {"error": err_msg, "exit_code": code},
        )

    if "issues" not in payload or not isinstance(payload["issues"], list):
        return (
            False,
            f"doctor printed malformed report (exit {code}): missing issues list",
            {"exit_code": code},
        )

    issues = [str(item) for item in payload["issues"]]
    own = [item for item in issues if item.startswith(ISSUE_PREFIX)]
    others = [item for item in issues if not item.startswith(ISSUE_PREFIX)]
    detail = {
        "issues": [redact_text(item)[:200] for item in others],
        "own_issues": len(own),
        "exit_code": code,
    }

    if others:
        return False, f"{len(others)} doctor issue(s): {redact_text(others[0])[:160]}", detail

    # Enforce allowed exits BEFORE own-only exemption:
    # Only exit 0 (clean) and exit 1 (reported issues) are allowed doctor exits.
    # Timeouts (124), signals (137), usage errors (2), or unexpected exits must never pass.
    if code not in (0, 1):
        first_err = redact_text(err.strip().splitlines()[-1] if err.strip() else f"exit {code}")[
            :200
        ]
        detail["error"] = first_err
        return (False, f"doctor exited (exit {code}): {first_err[:160]}".strip(), detail)

    if len(own) > 0:
        return True, "no doctor issue", detail

    if code != 0:
        first_err = redact_text(err.strip().splitlines()[-1] if err.strip() else f"exit {code}")[
            :200
        ]
        detail["error"] = first_err
        return (
            False,
            f"doctor exited (exit {code}) without reporting issues: {first_err[:160]}".strip(),
            detail,
        )

    return True, "no doctor issue", detail


def judge_choose(code: int, out: str, err: str) -> tuple[bool, str, dict[str, Any]]:
    payload = _json(out)
    if code != 0 or not isinstance(payload, dict):
        first = err.strip().splitlines()[-1] if err.strip() else ""
        return False, f"not implemented: exit {code} {redact_text(first)[:160]}".strip(), {}
    selected = payload.get("selected")
    if not selected:
        return False, f"no route selected: {payload.get('reason', 'unknown')}", {}
    return True, "route selected", {"selected": selected}


_INVALID_ROUTE_NAMES = frozenset(
    {
        "no_eligible_target",
        "none",
        "null",
        "undefined",
        "error",
        "failed",
        "failure",
        "timeout",
        "unknown",
    }
)

_ROUTE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9_.:/-]*[a-zA-Z0-9])?$")


def _is_valid_route_name(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    candidate = name.strip()
    if not candidate or len(candidate) > 256:
        return False
    if candidate.lower() in _INVALID_ROUTE_NAMES:
        return False
    return bool(_ROUTE_NAME_PATTERN.match(candidate))


def judge_route(code: int, out: str, err: str) -> tuple[bool, str, dict[str, Any]]:
    stripped = out.strip()
    if not stripped:
        return (
            False,
            f"no route (exit {code}): blank output",
            {"model": None, "reason": "blank output"},
        )

    last_line = stripped.splitlines()[-1].strip()

    if stripped.startswith("{") or last_line.startswith("{"):
        target_str = stripped if stripped.startswith("{") else last_line
        payload = _json(target_str)
        if not isinstance(payload, dict):
            return (
                False,
                f"no route (exit {code}): malformed JSON",
                {"model": None, "reason": "malformed JSON"},
            )

        model = payload.get("model")
        reason = payload.get("reason")
        detail = {"model": model, "reason": reason}
        if code != 0 or not model or model == "no_eligible_target":
            return (
                False,
                f"no route (exit {code}): {reason or model or 'unspecified error'}",
                detail,
            )
        if not _is_valid_route_name(model):
            return False, f"no route (exit {code}): invalid route: {model}", detail
        return True, f"routed to {model}", detail

    if code != 0:
        err_msg = err.strip().splitlines()[-1] if err.strip() else last_line
        return (
            False,
            f"no route (exit {code}): {redact_text(err_msg)[:160]}",
            {"model": None, "reason": err_msg},
        )

    model = last_line
    if not _is_valid_route_name(model):
        route_reason = model if model.lower() in _INVALID_ROUTE_NAMES else "invalid route name"
        detail = {"model": model, "reason": route_reason}
        return False, f"no route (exit 0): {route_reason}: {model}", detail

    detail = {"model": model, "reason": None}
    return True, f"routed to {model}", detail


def judge_prime(code: int, out: str, err: str) -> tuple[bool, str, dict[str, Any]]:
    payload = _json(out)
    if code != 0 or not isinstance(payload, dict):
        return False, f"harness prime status failed (exit {code})", {}
    detail = {key: payload.get(key) for key in ("enabled", "provider", "base_url", "integration")}
    if not payload.get("enabled"):
        return False, "Prime is not pointed at verdict serve", detail
    return True, "Prime uses verdict serve", detail


# ---------------------------------------------------------------------------
# Eligibility (A3)
# ---------------------------------------------------------------------------


def free_eligible(verdicts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Verdict's eligible FREE routes that can call tools."""
    return [
        v
        for v in verdicts
        if v.get("capacity_class") == "free"
        and v.get("reached") in _ELIGIBLE_STAGES
        and v.get("supports_tools") is True
    ]


def gateway_get(base_url: str, api_key: str | None, path: str, timeout: float = 60) -> Any:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(base_url.rstrip("/") + path, headers=headers)
    with open_no_redirect(request, timeout=timeout) as response:
        return json.loads(response.read())


def eligibility_check(gateway: str, api_key: str | None, *, seed: str) -> Check:
    from verdict.acceptance_toolcheck import (
        cooling_providers,
        gateway_post,
        inactive_providers,
        measure,
    )

    args = ["eligibility", "--json", "--gateway", gateway]
    started = time.monotonic()
    code, out, err = run_cli(args, 900, gateway_env(gateway))
    check = Check(
        "eligibility",
        ["verdict", *args],
        False,
        "",
        exit_code=code,
        stdout_sha256=_digest(out),
        stderr_sha256=_digest(err),
    )
    payload = _json(out)
    verdicts = payload.get("verdicts") if isinstance(payload, dict) else None
    if code != 0 or not isinstance(verdicts, list):
        check.reason = f"eligibility failed (exit {code})"
        check.detail = {"stderr_tail": _tail(err)}
    else:
        listed = free_eligible(verdicts)
        unproven = sorted(
            str(v["route_id"])
            for v in listed
            if not (v.get("agentic_capability") or {}).get("qualified")
        )
        check.detail = {
            "summary": payload.get("summary"),
            "eligible_free": len(listed),
            "listed_without_agentic_proof": unproven,
        }
        try:
            synced = gateway_get(gateway, api_key, "/api/synced-available-models")
            providers = gateway_get(gateway, api_key, "/api/providers")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            check.reason = f"gateway evidence unavailable: {type(exc).__name__}"
        else:
            # /v1/models is a second served-set source, fetched separately: the
            # synced endpoint can omit an active provider (observed: ollama-cloud)
            # that /v1/models still lists as live, and each source also carries
            # ids the other lacks. A failure here must not skip the whole check
            # (A3 still runs the independent probe) -- it only drops back to the
            # synced-only served set the check used before.
            served_set_source = "synced+v1_models"
            try:
                catalog: Mapping[str, Any] | None = gateway_get(gateway, api_key, "/v1/models")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                catalog = None
                served_set_source = "synced_only"
                check.detail["v1_models_error"] = type(exc).__name__
            connections = providers.get("connections") if isinstance(providers, dict) else []
            cooling = cooling_providers(connections or [], datetime.now(timezone.utc))
            inactive = inactive_providers(connections or [])
            # Runs even with nothing listed: the sample shows what Verdict misses.
            quality = measure(
                listed,
                verdicts,
                synced=synced if isinstance(synced, dict) else {},
                catalog=catalog if isinstance(catalog, dict) else None,
                cooling=cooling,
                inactive=inactive,
                post=gateway_post(gateway, api_key, timeout=60),
                seed=seed,
            )
            quality["served_set_source"] = served_set_source
            check.detail["independent_check"] = quality
            precision, recall = quality["precision"], quality["recall_estimate"]
            failures = []
            if not listed:
                failures.append("0 FREE tool-capable routes eligible")
            if unproven:
                failures.append(f"{len(unproven)} listed without agentic proof")
            if precision is None or precision < quality["precision_min"]:
                failures.append(f"precision {_pct(precision)} < {_pct(quality['precision_min'])}")
            # BOD-342 (2026-10-20): recall is a reported trend, never a failure.
            interval = quality.get("unlisted_wilson") or (None, None)
            check.detail["recall_trend"] = {
                "estimate": recall,
                "n": quality.get("unlisted_measured"),
                "passes": quality.get("unlisted_passes"),
                "wilson_low": interval[0],
                "wilson_high": interval[1],
                "population_before": quality.get("population_before"),
                "population_after": quality.get("population_after"),
            }
            trend = f"recall {_pct(recall)} (trend, {quality.get('unlisted_passes')}/"
            trend += f"{quality.get('unlisted_measured')} unlisted pass)"
            check.passed = not failures
            check.reason = (
                "; ".join([*failures, trend])
                if failures
                else (f"{len(listed)} eligible; precision {_pct(precision)}, {trend}")
            )
        if not listed and not check.reason.startswith("0 FREE"):
            check.reason = "0 FREE tool-capable routes eligible; " + check.reason
    check.seconds = round(time.monotonic() - started, 1)
    return check


def _pct(value: float | None) -> str:
    return "unmeasured" if value is None else f"{value:.0%}"


# ---------------------------------------------------------------------------
# serve: one real request through verdict serve
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve_check(gateway: str, *, startup_seconds: float = 120) -> Check:
    """Start ``verdict serve`` on a free loopback port, send one chat request, stop it."""
    # A file, not a pipe: an unread pipe can fill and block serve mid-request.
    with tempfile.TemporaryFile(mode="w+") as log:
        return _serve_check(gateway, startup_seconds, log)


def _serve_check(gateway: str, startup_seconds: float, log: IO[str]) -> Check:
    port = _free_port()
    token = secrets.token_urlsafe(24)
    # Authenticated serve needs a durable receipts DB; the job's own file keeps
    # its receipts out of the operator's ledger.
    receipts = acceptance_dir() / "serve-receipts.db"
    env = {
        **gateway_env(gateway),
        "LLMGATE_AUTH_TOKEN": token,
        "VERDICT_RECEIPTS_DB": str(receipts),
    }
    args = ["serve", "--port", str(port), "--host", "127.0.0.1"]
    started = time.monotonic()
    check = Check("serve", ["verdict", *args, "+ POST /v1/chat/completions"], False, "")
    process = subprocess.Popen(
        [sys.executable, "-m", "verdict", *args],
        stdout=subprocess.DEVNULL,
        stderr=log,
        text=True,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        while time.monotonic() - started < startup_seconds and process.poll() is None:
            try:
                with open_no_redirect(urllib.request.Request(base + "/health"), timeout=2):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(1)
        else:
            check.reason = (
                f"serve exited {process.returncode}"
                if process.poll() is not None
                else f"serve not up after {startup_seconds:g}s"
            )
            return check
        body = {
            "model": "default",
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "max_tokens": 16,
        }
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with open_no_redirect(request, timeout=180) as response:
                status, raw = response.status, response.read().decode("utf-8", "replace")
                served_by = response.headers.get("x-verdict-model") or ""
        except urllib.error.HTTPError as exc:
            status, raw, served_by = exc.code, exc.read().decode("utf-8", "replace"), ""
        except (OSError, urllib.error.URLError) as exc:
            status, raw, served_by = 0, f"{type(exc).__name__}: {exc}", ""
        check.exit_code = status
        check.stdout_sha256 = _digest(raw)
        reply = _json(raw)
        content = None
        if isinstance(reply, dict) and reply.get("choices"):
            content = (reply["choices"][0].get("message") or {}).get("content")
        check.detail = {
            "http_status": status,
            "model": (reply or {}).get("model"),
            "served_by": served_by,
        }
        if status == 200 and content is not None:
            check.passed, check.reason = True, f"HTTP 200 from {check.detail['model']}"
        else:
            check.reason = f"HTTP {status}: {redact_text(raw)[:160]}"
        return check
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.seek(0)
        err = log.read()
        check.stderr_sha256 = _digest(err)
        if not check.passed and err:
            check.detail["stderr_tail"] = _tail(err)
        check.seconds = round(time.monotonic() - started, 1)


# ---------------------------------------------------------------------------
# Run, keep, notify
# ---------------------------------------------------------------------------


@contextmanager
def _run_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AcceptanceBusyError("another acceptance run is active") from exc
        yield


def _source_sha() -> str | None:
    root = Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _guarded(name: str, run: Callable[[], Check]) -> Check:
    """A check that raises is a FAIL in the result, never a lost result file."""
    try:
        return run()
    except Exception as exc:
        return Check(
            name, [], False, f"check crashed: {type(exc).__name__}: {_tail(str(exc), 160)}"
        )


def run_checks(gateway: str, api_key: str | None, *, seed: str) -> list[Check]:
    def cli(name: str, args: list[str], timeout: float, judge: Judge) -> Callable[[], Check]:
        return lambda: _cli_check(name, args, timeout, judge, gateway)

    plan: list[tuple[str, Callable[[], Check]]] = [
        ("doctor", cli("doctor", ["doctor", "--json"], 600, judge_doctor)),
        ("eligibility", lambda: eligibility_check(gateway, api_key, seed=seed)),
        (
            "choose",
            cli(
                "choose",
                ["choose", "--task-class", "implementation", "--requires", "tools,code", "--json"],
                180,
                judge_choose,
            ),
        ),
        ("route", cli("route", ["route", "--terse", ROUTE_TASK], 300, judge_route)),
        ("serve", lambda: serve_check(gateway)),
        (
            "harness-prime",
            cli("harness-prime", ["harness", "prime", "status", "--json"], 60, judge_prime),
        ),
    ]
    return [_guarded(name, run) for name, run in plan]


def is_green(checks: Sequence[Check]) -> bool:
    """Every check passed, and the eligibility check saw an eligible FREE route."""
    eligibility = next((c for c in checks if c.name == "eligibility"), None)
    return (
        eligibility is not None
        and int(eligibility.detail.get("eligible_free") or 0) > 0
        and all(c.passed for c in checks)
    )


def run_acceptance(
    *,
    gateway: str | None = None,
    notify: str = "off",
    email: str | None = None,
    keep: int = KEEP_RESULTS,
    home: Path | None = None,
) -> dict[str, Any]:
    """Run every check once, write the result file, and update the alert state."""
    from verdict import __version__
    from verdict.credentials_store import resolve_api_key
    from verdict.orchestration.run import DEFAULT_GATEWAY

    if notify not in NOTIFY_MODES:
        raise ValueError(f"notify must be one of {', '.join(NOTIFY_MODES)}")
    directory = acceptance_dir(home)
    gateway = gateway or os.environ.get("OMNIROUTE_BASE_URL") or DEFAULT_GATEWAY
    with _run_lock(directory):
        started = datetime.now(timezone.utc)
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        checks = run_checks(gateway, resolve_api_key(), seed=stamp)
        green = is_green(checks)
        eligibility = next(c for c in checks if c.name == "eligibility")
        result: dict[str, Any] = {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "green" if green else "red",
            "eligible_free": int(eligibility.detail.get("eligible_free") or 0),
            "failed": [c.name for c in checks if not c.passed],
            "verdict_version": __version__,
            "source_sha": _source_sha(),
            "python": sys.executable,
            "gateway": gateway,
            "checks": [asdict(c) for c in checks],
        }
        path = directory / f"{stamp}.json"
        result["path"] = str(path)
        result["notification"] = update_alert_state(directory, result, notify=notify, email=email)
        # Doctor may read while we write: replace atomically.
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        pending.replace(path)
        prune(directory, keep)
    return result
