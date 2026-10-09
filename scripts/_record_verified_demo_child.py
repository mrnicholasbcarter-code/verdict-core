#!/usr/bin/env python3
"""Child process for the verified-models + bootstrap recording.

Starts a LOCAL, LOOPBACK-ONLY fixture HTTP server (no real provider is ever
reached) that serves canned ``/v1/models`` and ``/api/providers`` responses,
seeds a FIXTURE health cache (VERIFIED/STALE/FAILED mix), points Prime's and
Claude's settings at throwaway homes, then runs the real interactive home
prompt (prompt_toolkit) exactly what a user gets on a terminal. Only
cursor-position requests are disabled, because the recording pty does not
answer them.

Everything here is isolated: HOME, VERDICT_HOME and PRIME_AGENT_CODING_AGENT_DIR
are all set by the parent recorder to throwaway directories before this
process starts.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verdict.actions.verified_models import StorePaths

NOW = datetime.now(timezone.utc)


def _row(route_id: str, *, tools: bool = True) -> dict:
    return {
        "id": route_id,
        "owned_by": route_id.split("/", 1)[0],
        "context_length": 200_000,
        "capabilities": {"tool_calling": tools},
    }


def _conn(provider: str, *, active: bool = True) -> dict:
    return {
        "provider": provider,
        "isActive": active,
        "testStatus": "ok",
        "authType": "oauth",
        "plan_label": "Max",
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _at_rest(
    route_id: str,
    *,
    checked_at: datetime,
    healthy: bool = True,
    category: str = "ok",
    chat_ok: bool = True,
    tool_ok: bool = True,
    identity: str = "verified",
    until: datetime | None = None,
    **extra: object,
) -> dict:
    entry = {
        "route_id": route_id,
        "category": category,
        "checked_at": _iso(checked_at),
        "until": _iso(until or checked_at),
        "consecutive_failures": 0,
        "chat_ok": chat_ok,
        "tool_ok": tool_ok,
        "healthy": healthy,
        "identity": identity,
    }
    entry.update(extra)
    return entry


FIXTURE_INVENTORY = [_row("cc/sonnet"), _row("cc/opus"), _row("gc/grok"), _row("kr/kimi")]
FIXTURE_CONNECTIONS = [_conn("cc"), _conn("gc"), _conn("kr")]


class _FixtureGatewayHandler(http.server.BaseHTTPRequestHandler):
    """Loopback-only fixture gateway: canned /v1/models and /api/providers.

    No real provider, credential or network call is ever made; this process
    answers its own requests with the fixture rows defined above.
    """

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models"):
            self._send({"data": FIXTURE_INVENTORY})
        elif self.path.startswith("/api/providers"):
            self._send({"connections": FIXTURE_CONNECTIONS})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # no access log in the recording


def start_fixture_gateway() -> str:
    """Bind a loopback-only fixture HTTP server and return its base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}"


def seed_fixtures(gateway: str) -> None:
    """Write a fixture health cache and Prime/Claude settings. No network, no live probes."""
    verdict_home = Path(os.environ["VERDICT_HOME"])
    store = StorePaths.defaults(verdict_home)
    store.health_cache.parent.mkdir(parents=True, exist_ok=True)
    store.health_cache.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "routes": {
                    e["route_id"]: e
                    for e in (
                        _at_rest("cc/sonnet", checked_at=NOW - timedelta(seconds=60)),
                        _at_rest("cc/opus", checked_at=NOW - timedelta(seconds=900)),
                        _at_rest(
                            "gc/grok",
                            checked_at=NOW - timedelta(seconds=30),
                            healthy=False,
                            category="timeout",
                            chat_ok=False,
                            tool_ok=False,
                            identity="",
                            until=NOW + timedelta(seconds=60),
                            http_status=504,
                        ),
                    )
                },
            }
        )
    )
    prime_dir = Path(os.environ["PRIME_AGENT_CODING_AGENT_DIR"])
    prime_dir.mkdir(parents=True, exist_ok=True)
    prime_dir.chmod(0o700)
    (prime_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "omniroute": {
                        "api": "openai-completions",
                        "baseUrl": gateway + "/v1",
                        "apiKey": "fixture-demo-key-not-a-secret",
                        "models": [{"id": rid, "name": rid} for rid in ("cc/sonnet", "cc/opus")],
                    }
                },
                "enabledModels": ["cc/sonnet"],
            }
        )
    )
    (prime_dir / "models.json").chmod(0o600)
    (prime_dir / "settings.json").write_text(json.dumps({"enabledModels": ["cc/sonnet"]}))
    (prime_dir / "settings.json").chmod(0o600)
    claude_home = Path(os.environ["CLAUDE_HOME"])
    claude_home.mkdir(parents=True, exist_ok=True)
    (claude_home / "settings.json").write_text(json.dumps({}))


gateway_url = start_fixture_gateway()
os.environ["VERDICT_GATEWAY"] = gateway_url
seed_fixtures(gateway_url)

import verdict.home as _h  # noqa: E402

sys.exit(_h.run_home(gateway=gateway_url))
