"""First-party remaining-quota probes from well-known credential files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from verdict.live_routing import UsageSnapshot

_TIMEOUT = 4.0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _codex_token() -> str | None:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    auth = _read_json(home / "auth.json")
    if not auth:
        return None
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        access = tokens.get("access_token")
        if isinstance(access, str) and access:
            return access
    token = auth.get("access_token")
    return token if isinstance(token, str) and token else None


def _claude_token() -> str | None:
    creds = _read_json(Path.home() / ".claude" / ".credentials.json")
    if not creds:
        return None
    oauth = creds.get("claudeAiOauth") if isinstance(creds.get("claudeAiOauth"), dict) else creds
    for key in ("accessToken", "access_token", "token"):
        value = oauth.get(key) if isinstance(oauth, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def _fetch_json(url: str, token: str, *, extra_headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = httpx.get(url, headers=headers, timeout=_TIMEOUT)
        if response.status_code >= 400:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _codex_snapshot() -> UsageSnapshot | None:
    token = _codex_token()
    if not token:
        return None
    payload = _fetch_json("https://chatgpt.com/backend-api/wham/usage", token)
    if not payload:
        return None
    rate = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else payload
    primary = rate.get("primary_window") if isinstance(rate, dict) else None
    used = None
    remaining = None
    if isinstance(primary, dict):
        used = primary.get("used_percent")
        remaining = primary.get("remaining_percent")
    exhausted = bool(remaining == 0 or used == 100)
    return UsageSnapshot("codex", "oauth-file", _pct(used), _pct(remaining), None, exhausted)


def _claude_snapshot() -> UsageSnapshot | None:
    token = _claude_token()
    if not token:
        return None
    payload = _fetch_json(
        "https://api.anthropic.com/api/oauth/usage",
        token,
        extra_headers={"anthropic-beta": "oauth-2024-04-01"},
    )
    if not payload:
        return None
    five = payload.get("five_hour") if isinstance(payload.get("five_hour"), dict) else payload
    used = five.get("utilization") if isinstance(five, dict) else None
    remaining = None if used is None else max(0.0, 1.0 - float(used))
    exhausted = bool(used is not None and float(used) >= 1.0)
    return UsageSnapshot("claude", "oauth-file", _pct(used), remaining, None, exhausted)


def _openrouter_snapshot() -> UsageSnapshot | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    payload = _fetch_json("https://openrouter.ai/api/v1/credits", key)
    if not payload:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    total = data.get("total_credits") if isinstance(data, dict) else None
    remaining = data.get("total_usage") if isinstance(data, dict) else None
    exhausted = bool(total == 0)
    return UsageSnapshot("openrouter", "env", None, _pct(remaining), None, exhausted)


def _pct(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _xai_snapshot() -> UsageSnapshot | None:
    key = os.environ.get("XAI_MANAGEMENT_API_KEY")
    team = os.environ.get("XAI_TEAM_ID")
    if not key or not team:
        return None
    url = f"https://management-api.x.ai/v1/billing/teams/{team}/prepaid/balance"
    payload = _fetch_json(url, key)
    if not payload:
        return None
    remaining = payload.get("remaining_balance")
    if remaining is None:
        remaining = payload.get("balance")
    try:
        left = float(remaining)
    except (TypeError, ValueError):
        return UsageSnapshot("xai", "env", None, None, None, False)
    return UsageSnapshot("xai", "env", None, left, None, left <= 0)


def collect_usage() -> list[UsageSnapshot]:
    snapshots: list[UsageSnapshot] = []
    for loader in (_codex_snapshot, _claude_snapshot, _openrouter_snapshot, _xai_snapshot):
        item = loader()
        if item is not None:
            snapshots.append(item)
    return snapshots
