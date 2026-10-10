"""Strict, private connections evidence for restricted-key gateway discovery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.availability import opaque_connection_token
from verdict.orchestration.contracts import OrchestrationError

MAX_AGE_SECONDS = 6 * 60 * 60
_TOKEN = re.compile(r"[a-zA-Z0-9_.:/-]{1,128}")
_OPAQUE = re.compile(r"conn:[0-9a-f]{12}")
_REQUIRED = {
    "provider",
    "authType",
    "isActive",
    "testStatus",
    "backoffLevel",
    "lastError",
    "plan_label",
    "rate_limited_until",
    "import_free_only",
}
_DATES = {"rateLimitedUntil", "lastErrorAt", "updatedAt", "lastTested", "observed_at"}
_ENUMS = {
    "authType": {"oauth", "apikey", "none"},
    "testStatus": {
        "success",
        "ok",
        "healthy",
        "active",
        "failed",
        "error",
        "unknown",
        "untested",
        "pending",
        "invalid",
        "rate_limited",
    },
    "scope_type": {"model", "account", "provider", "pool"},
    "lastError": {"rate_limited"},
    "plan_label": {"", "free"},
    "quota_window": {
        "daily",
        "weekly",
        "monthly",
        "hourly",
        "5h",
        "7d",
        "24h",
        "day",
        "week",
        "month",
    },
}
_ENUMS["quotaWindow"] = _ENUMS["quota_window"]


def _date(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return stamp


def _valid(field: str, value: Any) -> bool:
    if value is None:
        return field != "provider" and field in _REQUIRED | _DATES | set(_ENUMS) | {
            "quota_percent",
            "quotaRemainingPct",
        }
    if field in _ENUMS:
        return isinstance(value, str) and value in _ENUMS[field]
    if field in _DATES:
        try:
            _date(value)
            return True
        except (ValueError, TypeError):
            return False
    if field in {"id", "account_id", "pool_id"}:
        return isinstance(value, str) and _OPAQUE.fullmatch(value) is not None
    if field in {"provider", "model", "scope_id"}:
        return isinstance(value, str) and _TOKEN.fullmatch(value) is not None
    if field in {"isActive", "import_free_only"}:
        return type(value) is bool
    if field == "backoffLevel":
        return type(value) is int and value >= 0
    if field in {"quota_percent", "quotaRemainingPct"}:
        return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 100
    if field == "rate_limited_until":
        return isinstance(value, dict) and all(
            _valid("model", key) and _valid("observed_at", stamp) and stamp is not None
            for key, stamp in value.items()
        )
    return False


def sanitize_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Constrain retained evidence to tokens, typed facts and fixed categories."""
    for field in ("pool_id", "scope_id"):
        if item.get(field) and (
            field == "pool_id" or item.get("scope_type", "account") in {"account", "pool"}
        ):
            item[field] = opaque_connection_token(str(item[field]))
    item["plan_label"] = "free" if "free" in item.get("plan_label", "").lower() else ""
    return {field: value if _valid(field, value) else None for field, value in item.items()}


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def read_connections_snapshot(path: Path) -> list[dict[str, Any]]:
    """Reject stale, malformed or unsanitized evidence without HTTP fallback."""
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("snapshot too large")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "captured_at",
            "connections",
            "sha256",
        }:
            raise ValueError("invalid envelope")
        digest = data.pop("sha256")
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or digest != _digest(data)
        ):
            raise ValueError("invalid schema or digest")
        age = (datetime.now(timezone.utc) - _date(data["captured_at"])).total_seconds()
        if not 0 <= age <= MAX_AGE_SECONDS:
            raise ValueError("snapshot expired or future dated")
        rows = data["connections"]
        if not isinstance(rows, list):
            raise ValueError("connections must be an array")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not row.keys() >= _REQUIRED
                or not all(_valid(k, v) for k, v in row.items())
            ):
                raise ValueError("invalid connection evidence")
            scope = row.get("scope_type", "account")
            if (
                scope in {"account", "pool"}
                and row.get("scope_id") is not None
                and not _valid("id", row["scope_id"])
            ):
                raise ValueError("nonopaque scope id")
        return rows
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise OrchestrationError(
            "Invalid connections snapshot: missing, malformed, or older than 6 hours"
        ) from exc


def write_connections_snapshot(path: Path, connections: list[dict[str, Any]]) -> None:
    """Write only sanitized evidence, timestamp and digest, atomically at 0600."""
    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "connections": connections,
    }
    payload["sha256"] = _digest(payload)
    fd, name = tempfile.mkstemp(prefix=".connections-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, sort_keys=True, allow_nan=False)
        read_connections_snapshot(tmp)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
