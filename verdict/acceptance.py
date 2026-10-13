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
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _cli_check(name: str, args: list[str], timeout: float, judge: Judge) -> Check:
    started = time.monotonic()
    code, out, err = run_cli(args, timeout)
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
    """Doctor passes when it has no issue other than this job's own result."""
    payload = _json(out)
    if not isinstance(payload, dict):
        return False, f"doctor printed no JSON (exit {code})", {}
    issues = [str(item) for item in payload.get("issues") or []]
    own = [item for item in issues if item.startswith(ISSUE_PREFIX)]
    others = [item for item in issues if not item.startswith(ISSUE_PREFIX)]
    detail = {"issues": [redact_text(item)[:200] for item in others], "own_issues": len(own)}
    if others:
        return False, f"{len(others)} doctor issue(s): {redact_text(others[0])[:160]}", detail
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


def judge_route(code: int, out: str, err: str) -> tuple[bool, str, dict[str, Any]]:
    payload = _json(out.strip().splitlines()[-1] if out.strip() else "")
    model = payload.get("model") if isinstance(payload, dict) else None
    detail = {
        "model": model,
        "reason": payload.get("reason") if isinstance(payload, dict) else None,
    }
    if code != 0 or not model or model == "no_eligible_target":
        return False, f"no route (exit {code}): {detail['reason'] or model}", detail
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
    from verdict.acceptance_toolcheck import cooling_providers, gateway_post, measure

    args = ["eligibility", "--json", "--gateway", gateway]
    started = time.monotonic()
    code, out, err = run_cli(args, 900)
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
        if not listed:
            check.reason = "0 FREE tool-capable routes eligible"
        else:
            try:
                synced = gateway_get(gateway, api_key, "/api/synced-available-models")
                providers = gateway_get(gateway, api_key, "/api/providers")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                check.reason = f"gateway evidence unavailable: {type(exc).__name__}"
            else:
                connections = providers.get("connections") if isinstance(providers, dict) else []
                cooling = cooling_providers(connections or [], datetime.now(timezone.utc))
                quality = measure(
                    listed,
                    verdicts,
                    synced=synced if isinstance(synced, dict) else {},
                    cooling=cooling,
                    post=gateway_post(gateway, api_key, timeout=60),
                    seed=seed,
                )
                check.detail["independent_check"] = quality
                precision, recall = quality["precision"], quality["recall_estimate"]
                failures = []
                if unproven:
                    failures.append(f"{len(unproven)} listed without agentic proof")
                if precision is None or precision < quality["precision_min"]:
                    failures.append(
                        f"precision {_pct(precision)} < {_pct(quality['precision_min'])}"
                    )
                if recall is None or recall < quality["recall_min"]:
                    failures.append(f"recall {_pct(recall)} < {_pct(quality['recall_min'])}")
                check.passed = not failures
                check.reason = "; ".join(failures) or (
                    f"{len(listed)} eligible; precision {_pct(precision)}, recall {_pct(recall)}"
                )
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
    port = _free_port()
    token = secrets.token_urlsafe(24)
    env = {**os.environ, "LLMGATE_AUTH_TOKEN": token, "OMNIROUTE_BASE_URL": gateway}
    args = ["serve", "--port", str(port), "--host", "127.0.0.1"]
    started = time.monotonic()
    check = Check("serve", ["verdict", *args, "+ POST /v1/chat/completions"], False, "")
    process = subprocess.Popen(
        [sys.executable, "-m", "verdict", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        while time.monotonic() - started < startup_seconds and process.poll() is None:
            try:
                with urllib.request.urlopen(base + "/health", timeout=2):  # nosec B310
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
            with urllib.request.urlopen(request, timeout=180) as response:  # nosec B310
                status, raw = response.status, response.read().decode("utf-8", "replace")
                served_by = response.headers.get("x-verdict-model") or ""
        except urllib.error.HTTPError as exc:
            status, raw, served_by = exc.code, exc.read().decode("utf-8", "replace"), ""
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
            _, err = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            _, err = process.communicate()
        check.stderr_sha256 = _digest(err or "")
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
            raise RuntimeError("another acceptance run is active") from exc
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


def run_checks(gateway: str, api_key: str | None, *, seed: str) -> list[Check]:
    return [
        _cli_check("doctor", ["doctor", "--json"], 600, judge_doctor),
        eligibility_check(gateway, api_key, seed=seed),
        _cli_check("choose", ["choose", "--role", "implement", "--json"], 180, judge_choose),
        _cli_check("route", ["route", "--terse", ROUTE_TASK], 300, judge_route),
        serve_check(gateway),
        _cli_check("harness-prime", ["harness", "prime", "status", "--json"], 60, judge_prime),
    ]


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
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        prune(directory, keep)
    return result
