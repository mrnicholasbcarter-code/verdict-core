"""OmniRoute webhook receiver, local event log and registration.

OmniRoute signs each delivery with ``X-Webhook-Signature: sha256=<hex>``, an
HMAC-SHA256 of the raw body under the secret Verdict registered. Verified
``request.completed`` and ``request.failed`` deliveries are appended to a
bounded local log that the passive health refresh reads as chat-only liveness.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from verdict.orchestration.health_cache import PASSIVE_CLOCK_SKEW_SECONDS
from verdict.paths import verdict_home

RECEIVER_PATH = "/v1/omniroute/webhook"
CONFIG_FILENAME = "omniroute-webhook.json"
EVENTS_FILENAME = "omniroute-webhook-events.jsonl"
MAX_EVENT_LINES = 5000
# OmniRoute's request events are a few hundred bytes; the receiver is bearer-exempt.
MAX_BODY_BYTES = 64 * 1024
MAX_FIELD_CHARS = 256
MAX_FUTURE_SKEW = timedelta(seconds=PASSIVE_CLOCK_SKEW_SECONDS)
STORED_EVENTS = frozenset({"request.completed", "request.failed"})
SUBSCRIBED_EVENTS = ("request.completed", "request.failed")
DEFAULT_RECEIVER_URL = f"http://127.0.0.1:8000{RECEIVER_PATH}"


@dataclass(frozen=True)
class WebhookEvent:
    event: str
    timestamp: datetime
    received_at: datetime
    provider: str | None
    model: str | None
    latency_ms: float | None
    digest: str


def config_path() -> Path:
    return verdict_home() / CONFIG_FILENAME


def events_path() -> Path:
    return verdict_home() / EVENTS_FILENAME


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load_config() -> dict[str, Any]:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(config: Mapping[str, Any]) -> None:
    _write_private(config_path(), json.dumps(dict(config), indent=2, sort_keys=True) + "\n")


@contextmanager
def _events_lock() -> Iterator[None]:
    path = events_path().with_name(EVENTS_FILENAME + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[len("sha256=") :].strip().lower(), expected)


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD_CHARS:
        return None
    return value.strip()


def _latency(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _read_lines() -> list[str]:
    try:
        return events_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _store(row: Mapping[str, Any]) -> bool:
    """Append ``row`` unless its digest is already logged; keep the newest lines."""
    line = json.dumps(dict(row), sort_keys=True)
    with _events_lock():
        lines = _read_lines()
        for existing in lines:
            try:
                if json.loads(existing).get("digest") == row["digest"]:
                    return False
            except (ValueError, AttributeError):
                continue
        lines.append(line)
        path = events_path()
        if len(lines) > MAX_EVENT_LINES or not path.exists():
            _write_private(path, "\n".join(lines[-MAX_EVENT_LINES:]) + "\n")
        else:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    return True


def accept_delivery(
    raw_body: bytes, headers: Mapping[str, str], *, now: datetime | None = None
) -> tuple[int, dict[str, Any]]:
    """Verify, parse and store one delivery; return ``(http_status, body)``."""
    if len(raw_body) > MAX_BODY_BYTES:
        return 413, {"accepted": False, "reason": "body_too_large"}
    now = now or datetime.now(timezone.utc)
    secret = _load_config().get("secret")
    lowered = {key.lower(): value for key, value in headers.items()}
    if not isinstance(secret, str) or not secret:
        return 401, {"accepted": False, "reason": "not_configured"}
    if not verify_signature(raw_body, lowered.get("x-webhook-signature"), secret):
        return 401, {"accepted": False, "reason": "invalid_signature"}
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return 400, {"accepted": False, "reason": "invalid_json"}
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        return 400, {"accepted": False, "reason": "invalid_payload"}
    timestamp = _parse_time(payload.get("timestamp"))
    if timestamp is None:
        return 400, {"accepted": False, "reason": "invalid_timestamp"}
    if timestamp > now + MAX_FUTURE_SKEW:
        return 200, {"accepted": False, "reason": "future_timestamp"}
    event = payload["event"]
    if event not in STORED_EVENTS:
        return 200, {"accepted": True, "reason": "not_stored"}
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    row = {
        "event": event,
        "timestamp": _iso(timestamp),
        "received_at": _iso(now),
        "provider": _text(data.get("provider")),
        "model": _text(data.get("model")),
        "latency_ms": _latency(data.get("latencyMs")),
        "digest": hashlib.sha256(raw_body).hexdigest(),
    }
    if not _store(row):
        return 200, {"accepted": False, "reason": "duplicate"}
    return 200, {"accepted": True, "reason": "stored"}


def _event_from_row(row: Any) -> WebhookEvent | None:
    if not isinstance(row, dict):
        return None
    timestamp = _parse_time(row.get("timestamp"))
    received_at = _parse_time(row.get("received_at"))
    event, digest = row.get("event"), row.get("digest")
    if timestamp is None or received_at is None or event not in STORED_EVENTS:
        return None
    if not isinstance(digest, str) or not digest:
        return None
    return WebhookEvent(
        event=event,
        timestamp=timestamp,
        received_at=received_at,
        provider=_text(row.get("provider")),
        model=_text(row.get("model")),
        latency_ms=_latency(row.get("latency_ms")),
        digest=digest,
    )


def read_events(*, since: datetime | None = None) -> list[WebhookEvent]:
    """Logged events in file order, optionally only those observed at or after ``since``."""
    events: list[WebhookEvent] = []
    for line in _read_lines():
        try:
            parsed = _event_from_row(json.loads(line))
        except ValueError:
            continue
        if parsed is not None and (since is None or parsed.timestamp >= since):
            events.append(parsed)
    return events


def webhook_status(*, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    config = _load_config()
    events = read_events()
    last = max((event.received_at for event in events), default=None)
    return {
        "configured": bool(config.get("webhook_id") and config.get("url") and config.get("secret")),
        "webhook_id": config.get("webhook_id"),
        "url": config.get("url"),
        "registration": config.get("registration"),
        "last_event_at": _iso(last) if last else None,
        "last_event_age_seconds": (now - last).total_seconds() if last else None,
        "events_total": len(events),
        "receiver_path": RECEIVER_PATH,
    }


def _origin(origin: str) -> str:
    base = origin.strip().rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def register_webhook(
    *,
    origin: str,
    management_token: str,
    url: str,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Register ``url`` with OmniRoute for request events; never return the secret.

    The URL is pre-flighted with ``/api/webhooks/validate-url``: OmniRoute refuses
    private and loopback receivers unless its operator allows them, and that
    refusal is reported as ``blocked``. ``dry_run`` runs the read-only checks,
    creates nothing and writes nothing.
    """
    config = _load_config()
    attempt: dict[str, Any] = {"url": url, "attempted_at": _iso(datetime.now(timezone.utc))}

    def finish(status: str, **fields: Any) -> dict[str, Any]:
        result = {**attempt, "status": status, **fields}
        if dry_run:
            return {**result, "dry_run": True}
        _save_config({**config, "registration": result})
        return result

    client = httpx.Client(
        base_url=_origin(origin),
        transport=transport,
        headers={"Authorization": f"Bearer {management_token}"},
        timeout=10.0,
    )
    try:
        with client:
            check = client.post("/api/webhooks/validate-url", json={"url": url})
            if check.status_code != 200:
                return finish("error", reason=f"validate_url_http_{check.status_code}")
            verdict_body = check.json()
            if not verdict_body.get("valid"):
                return finish("blocked", reason=str(verdict_body.get("reason") or "invalid_url"))
            listing = client.get("/api/webhooks")
            if listing.status_code != 200:
                return finish("error", reason=f"list_http_{listing.status_code}")
            existing = next(
                (
                    hook
                    for hook in listing.json().get("webhooks") or []
                    if isinstance(hook, dict) and hook.get("url") == url
                ),
                None,
            )
            if existing is not None:
                webhook_id = existing.get("id")
                if webhook_id == config.get("webhook_id") and config.get("secret"):
                    return finish("exists", webhook_id=webhook_id)
                return finish("exists_unknown_secret", webhook_id=webhook_id)
            if dry_run:
                return finish("would_register")
            secret = "whsec_" + secrets.token_hex(24)
            created = client.post(
                "/api/webhooks",
                json={
                    "url": url,
                    "events": list(SUBSCRIBED_EVENTS),
                    "secret": secret,
                    "description": "Verdict passive health evidence",
                },
            )
            if created.status_code not in {200, 201}:
                return finish("error", reason=f"create_http_{created.status_code}")
            webhook_id = (created.json().get("webhook") or {}).get("id")
            if not isinstance(webhook_id, str) or not webhook_id:
                return finish("error", reason="create_missing_id")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return finish("error", reason=type(exc).__name__)
    result = {**attempt, "status": "registered", "webhook_id": webhook_id}
    _save_config(
        {
            "webhook_id": webhook_id,
            "url": url,
            "secret": secret,
            "registered_at": attempt["attempted_at"],
            "registration": result,
        }
    )
    return result
