"""Acceptance results on disk, the alert state, the operator email, and doctor's view."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.paths import verdict_home
from verdict.security import redact_text

INTERVAL_SECONDS = 3600
ISSUE_PREFIX = "Live acceptance"


def acceptance_dir(home: Path | None = None) -> Path:
    return (home or verdict_home()) / "acceptance"


def result_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("2*.json") if p.is_file())


def prune(directory: Path, keep: int) -> None:
    files = result_files(directory)
    for old in files[: max(0, len(files) - keep)]:
        old.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_alert_state(
    directory: Path, result: Mapping[str, Any], *, notify: str, email: str | None
) -> dict[str, Any]:
    """One email when a green job is red for two runs in a row, one when it recovers.

    ``state.json`` keeps the last status, the red streak and whether the job
    has been green since the last alert, so every streak alerts at most once.
    A job that was never green does not alert: that red state is known.
    """
    state_path = directory / "state.json"
    state = _read_json(state_path)
    status = str(result["status"])
    red_streak = int(state.get("red_streak", 0)) + 1 if status == "red" else 0
    was_green = bool(state.get("seen_green")) or status == "green"
    alerted = bool(state.get("alerted"))
    event: str | None = None
    if status == "red" and red_streak >= 2 and state.get("seen_green") and not alerted:
        event, alerted = "regression", True
    elif status == "green" and alerted:
        event, alerted = "recovery", False
    outcome: dict[str, Any] = {"mode": notify, "event": event, "red_streak": red_streak}
    if event is not None and notify != "off":
        outcome.update(_notify(directory, result, event, notify=notify, email=email))
    state.update(
        {
            "last_status": status,
            "last_result": result.get("path"),
            "red_streak": red_streak,
            "seen_green": was_green,
            "alerted": alerted,
        }
    )
    if outcome.get("message_id") or outcome.get("dry_run_file"):
        state.setdefault("sent", []).append(
            {k: outcome.get(k) for k in ("event", "message_id", "dry_run_file")}
            | {"at": result.get("finished_at")}
        )
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return outcome


def email_payload(result: Mapping[str, Any], event: str, email: str) -> dict[str, Any]:
    checks = {c["name"]: c for c in result.get("checks", [])}
    lines = [
        f"Verdict live acceptance {event} on {socket.gethostname()}.",
        f"Status: {result.get('status')}; eligible FREE routes: {result.get('eligible_free')}.",
        f"Verdict {result.get('verdict_version')} at {result.get('source_sha') or 'unknown SHA'}.",
        f"Result file: {result.get('path')}",
        "",
    ]
    for name in result.get("failed") or []:
        lines.append(f"FAIL {name}: {checks.get(name, {}).get('reason', '')}")
    lines += ["", "Next: run `verdict doctor` on the host; the result file has each check."]
    subject = (
        f"Verdict acceptance RED: {', '.join(result.get('failed') or [])}"
        if event == "regression"
        else "Verdict acceptance recovered (green)"
    )
    return {
        "recipient_email": email,
        "subject": subject,
        "body": "\n".join(lines),
        "is_html": False,
    }


def _composio() -> str | None:
    return shutil.which("composio") or next(
        (str(p) for p in (Path.home() / ".local/bin/composio",) if p.is_file()), None
    )


def _notify(
    directory: Path, result: Mapping[str, Any], event: str, *, notify: str, email: str | None
) -> dict[str, Any]:
    if not email:
        return {"error": "no recipient: pass --email or set notify_email in verdict.yaml"}
    payload = email_payload(result, event, email)
    stamp = Path(str(result.get("path"))).stem
    path = directory / f"email-{stamp}-{event}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    path.chmod(0o600)
    if notify == "dry-run":
        return {"dry_run_file": str(path)}
    return send_email(path)


def send_email(payload_path: Path) -> dict[str, Any]:
    """Send through the operator's approved path: Composio ``GMAIL_SEND_EMAIL``."""
    binary = _composio()
    if binary is None:
        return {"error": "composio CLI not found"}
    try:
        done = subprocess.run(
            [binary, "execute", "GMAIL_SEND_EMAIL", "-d", f"@{payload_path}"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"composio: {type(exc).__name__}"}
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = {}
    data = reply.get("data") if isinstance(reply, dict) else None
    if done.returncode != 0 or not isinstance(data, dict) or not reply.get("successful"):
        return {
            "error": f"composio exit {done.returncode}: {redact_text((done.stderr or done.stdout).strip()[-200:])}"
        }
    return {"message_id": data.get("id"), "thread_id": data.get("threadId")}


def configured_email() -> str | None:
    """``notify_email`` from verdict.yaml, the operator's notification address."""
    import yaml

    from verdict.setup_config import config_path

    try:
        config = yaml.safe_load(config_path().read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    value = config.get("notify_email") if isinstance(config, dict) else None
    return str(value).strip() or None if value else None


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def doctor_check(
    *, home: Path | None = None, now: datetime | None = None
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Newest acceptance result: ``(row, issues, warnings)``.

    Red or older than 2 x the hourly interval is an issue; no result yet is a
    warning (the timer runs a few minutes after install).
    """
    files = result_files(acceptance_dir(home))
    if not files:
        return (
            {"state": "none"},
            [],
            ["Live acceptance has no result yet: run 'verdict acceptance run'"],
        )
    newest = _read_json(files[-1])
    current = now or datetime.now(timezone.utc)
    try:
        finished = datetime.fromisoformat(str(newest.get("finished_at")))
        age = (current - finished).total_seconds()
    except ValueError:
        age = None
    row = {
        "state": newest.get("status", "unreadable"),
        "path": str(files[-1]),
        "age_seconds": None if age is None else round(age),
        "eligible_free": newest.get("eligible_free"),
        "failed": newest.get("failed", []),
    }
    issues: list[str] = []
    if newest.get("status") != "green":
        failed = ", ".join(
            f"{c.get('name')} ({c.get('reason')})"
            for c in newest.get("checks", [])
            if not c.get("passed")
        )
        issues.append(f"{ISSUE_PREFIX} is red ({files[-1].name}): {failed or 'unreadable result'}")
    if age is None or age > 2 * INTERVAL_SECONDS:
        observed = "unknown age" if age is None else f"{age / 3600:.1f} h old"
        issues.append(
            f"{ISSUE_PREFIX} result is stale ({observed}, limit {2 * INTERVAL_SECONDS / 3600:g} h); "
            "check: journalctl --user -u verdict-acceptance.service"
        )
    return row, issues, []
