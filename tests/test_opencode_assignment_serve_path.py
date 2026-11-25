"""The executor's signed assignment, through the real ``verdict serve`` app (#1325).

A loopback ``uvicorn`` server runs the real app (the real ``caller_authentication`` middleware,
the real assigned lane, the real relay and evidence store) with a recording fake upstream.  The
real ``OpenCodeExecutor`` signs the assignment and launches a fake worker, a script that sends
one request to that server with the credential it was given, exactly as OpenCode does (the shape
of OpenCode's own request was captured against a loopback stand-in on 2026-11-25: ``POST
/v1/chat/completions`` with ``Authorization: Bearer <apiKey>``, ``stream: true``, ``tools``).

What this proves, with no live call:

* the token the executor signs is accepted by serve's own verifier, the request is served on the
  signed route, and serve's evidence names the run, the node and the route;
* the waiver serve records for a streaming request is read back onto the attempt's terminal (and
  from there into the run receipt), and an attempt without the waiver does not carry it;
* an expired, tampered or foreign-key assignment gets serve's refusal, nothing reaches the
  upstream, no evidence is written, and the attempt ends as a configuration block rather than
  a provider denial.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import textwrap
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import verdict.api as api
from tests.relay_proof_helpers import ASSIGNMENT_KEY, ASSIGNMENT_SERVE_TOKEN
from tests.test_assigned_lane import (  # noqa: F401  (the fixtures are used by name)
    ORIGIN,
    ROUTES,
    Wired,
    body_for,
    build_service,
    now,
    store,
)
from verdict.orchestration.executor_opencode import OpenCodeExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.proxy import UpstreamProxy

ROUTE = ROUTES[2]
RUN_ID = "run-e2e"
NODE_ID = "node-e2e"
TOOLS = [{"type": "function", "function": {"name": "edit", "parameters": {"type": "object"}}}]


class LiveServe:
    """The real FastAPI app on a loopback port, in a thread."""

    def __init__(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            api.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the loopback serve did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


WORKER = """
import sys, os, json, urllib.request, urllib.error
if "--version" in sys.argv:
    sys.exit(0)
sys.stdin.read()
token = os.environ["VERDICT_WORKER_API_KEY"]
if {tamper!r}:
    head, payload, tag = token.split(".")
    payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    token = ".".join((head, payload, tag))
request = urllib.request.Request(
    {url!r} + "/v1/chat/completions",
    data=json.dumps({body!r}).encode(),
    headers={{"Authorization": "Bearer " + token, "Content-Type": "application/json"}},
    method="POST",
)
try:
    reply = urllib.request.urlopen(request, timeout=60)
    status, text = reply.status, reply.read().decode()
except urllib.error.HTTPError as exc:
    status, text = exc.code, exc.read().decode()
open({out!r}, "w").write(json.dumps({{"status": status, "body": text, "token": token}}))
if status in (401, 403):
    message = json.loads(text)["error"]["message"]
    sys.stdout.write(json.dumps({{"type": "error", "sessionID": "ses_x", "error": {{
        "type": "provider.auth", "message": message, "status": status}}}}) + "\\n")
    sys.exit(1)
sys.exit(0)
"""


class Harness:
    def __init__(self, wired: Wired, serve: LiveServe, origin: str, tmp_path: Path) -> None:
        self.wired, self.serve, self.origin, self.tmp_path = wired, serve, origin, tmp_path
        self.db = tmp_path / "serve-receipts.db"
        self.out = tmp_path / "worker-out.json"

    def worker(self, body: dict[str, Any], *, tamper: bool = False) -> str:
        script = self.tmp_path / "fake-opencode"
        script.write_text(
            "#!/usr/bin/env python3\n"
            + textwrap.dedent(WORKER).format(
                url=self.origin, body=body, out=str(self.out), tamper=tamper
            )
        )
        script.chmod(0o755)
        return str(script)

    def run(self, body: dict[str, Any], *, tamper: bool = False, **overrides: Any) -> Any:
        cwd = self.tmp_path / "work"
        cwd.mkdir(exist_ok=True)
        executor = OpenCodeExecutor(
            opencode_bin=self.worker(body, tamper=tamper),
            serve_base_url=self.origin,
            receipts_db_provider=lambda: self.db,
            **overrides,
        )
        return asyncio.run(
            executor.run(
                "task",
                route_id=ROUTE,
                cwd=cwd,
                timeout_seconds=60.0,
                run_id=RUN_ID,
                node_id=NODE_ID,
            )
        )

    def worker_saw(self) -> dict[str, Any]:
        return json.loads(self.out.read_text())

    def evidence_roots(self) -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT payload_json FROM receipts "
                "WHERE receipt_type = 'decision' AND parent_receipt_id IS NULL"
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(row[0]) for row in rows]


def _serving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811  (a fixture imported by name)
    store: Any,  # noqa: F811
    *,
    upstream_status: int | None = None,
) -> Iterator[Harness]:
    """The real app on a loopback port; with ``upstream_status`` its upstream answers that status
    and serve's own refusal text, as a provider that denies the account would."""
    db = tmp_path / "serve-receipts.db"
    monkeypatch.setenv("VERDICT_RECEIPTS_DB", str(db))
    wired = Wired(monkeypatch, tmp_path, build_service(now, store))
    if upstream_status is not None:

        async def denies(request: httpx.Request) -> httpx.Response:
            raw = await request.aread()
            wired.upstream.append(
                {
                    "headers": dict(request.headers),
                    "body": json.loads(raw) if raw else None,
                    "raw": raw.decode(),
                }
            )
            return httpx.Response(
                upstream_status,
                json={"error": {"message": "authentication failed", "type": "provider_auth"}},
            )

        monkeypatch.setattr(
            api,
            "_build_proxy",
            lambda: UpstreamProxy(
                ORIGIN, api_key="gateway-key", transport=httpx.MockTransport(denies)
            ),
        )
    serve = LiveServe()
    origin = serve.start()
    try:
        yield Harness(wired, serve, origin, tmp_path)
    finally:
        serve.stop()


@pytest.fixture
def harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811  (a fixture imported by name)
    store: Any,  # noqa: F811
) -> Iterator[Harness]:
    yield from _serving(monkeypatch, tmp_path, now, store)


@pytest.fixture(params=[401, 403])
def harness_upstream_denies(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: Any,  # noqa: F811
) -> Iterator[Harness]:
    yield from _serving(monkeypatch, tmp_path, now, store, upstream_status=request.param)


def test_the_signed_assignment_is_served_on_the_signed_route_and_the_waiver_is_read_back(
    harness: Harness,
) -> None:
    body = body_for(ROUTE, tools=TOOLS, stream=True)
    terminal = harness.run(body)
    assert terminal.ok, terminal

    # serve accepted the executor's assignment and sent the request upstream, once, on the route
    seen = harness.worker_saw()
    assert seen["status"] != 403, seen["body"]
    assert len(harness.wired.upstream) == 1
    upstream = harness.wired.upstream[0]
    assert upstream["body"]["model"] == ROUTE
    assert upstream["headers"]["authorization"] == "Bearer gateway-key"
    # neither the assignment nor the serve token went upstream
    assert seen["token"] not in upstream["raw"] + json.dumps(upstream["headers"])
    assert ASSIGNMENT_SERVE_TOKEN not in upstream["raw"] + json.dumps(upstream["headers"])

    # serve's own evidence names the route, the run and the node, and the waiver
    (root,) = harness.evidence_roots()
    receipt = root["routing_decision"]["receipt"]
    assert receipt["worker_assignment"]["route_id"] == ROUTE
    assert receipt["worker_assignment"]["run_id"] == RUN_ID
    assert receipt["worker_assignment"]["node_id"] == NODE_ID
    assert [w["requirement"] for w in receipt["requirement_waivers"]] == ["streaming"]

    # and the executor read that back onto the terminal, by digest, never by credential
    record = terminal.worker_assignment
    assert record["assignment_digest"] == receipt["worker_assignment"]["assignment_digest"]
    assert record["serve_receipts"] == {
        "status": "read",
        "reason": None,
        "requests": 1,
        "requests_waived": 1,
        "requirement_waivers": [
            {"requirement": "streaming", "reason": "verified_worker_assignment"}
        ],
    }
    assert seen["token"] not in json.dumps(record)


def test_a_request_that_needed_no_waiver_leaves_the_attempt_without_one(harness: Harness) -> None:
    terminal = harness.run(body_for(ROUTE, tools=TOOLS))  # not streaming
    assert terminal.ok, terminal
    assert len(harness.wired.upstream) == 1
    serve = terminal.worker_assignment["serve_receipts"]
    assert (serve["status"], serve["requests"], serve["requests_waived"]) == ("read", 1, 0)
    assert serve["requirement_waivers"] == []


def test_an_admitted_request_whose_upstream_says_authentication_failed_is_the_providers(
    harness_upstream_denies: Harness,
) -> None:
    """Serve forwards a provider's own 401 or 403 unchanged, so the very words serve uses to
    refuse an assignment can come from the route's provider.  Serve admitted this assignment (its
    receipts hold the request), so the denial is the provider's and recovery must see it as such,
    not as a configuration block."""
    harness = harness_upstream_denies
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True))
    seen = harness.worker_saw()
    assert seen["status"] in (401, 403)
    assert json.loads(seen["body"])["error"]["message"] == "authentication failed"
    assert len(harness.wired.upstream) == 1, "the request was not admitted and sent upstream"
    assert len(harness.evidence_roots()) == 1
    assert terminal.ok is False
    assert terminal.status_code == seen["status"]
    assert not terminal.error.startswith("harness_configuration:"), terminal.error
    serve = terminal.worker_assignment["serve_receipts"]
    assert (serve["status"], serve["requests"]) == ("read", 1)
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert verdict.category != "harness_configuration"


def _assert_refused_and_nothing_happened(harness: Harness, terminal: Any) -> None:
    seen = harness.worker_saw()
    assert seen["status"] == 403
    assert json.loads(seen["body"])["error"]["message"] == "authentication failed"
    assert harness.wired.upstream == [], "a refused assignment reached the upstream"
    assert harness.evidence_roots() == [], "serve wrote evidence for a refused assignment"
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: verdict serve refused"), terminal.error
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action, verdict.cooldown_seconds) == (
        "harness_configuration",
        "BLOCK",
        0,
    )
    serve = terminal.worker_assignment["serve_receipts"]
    assert (serve["status"], serve["requests"], serve["requests_waived"]) == ("read", 0, 0)


def test_an_expired_assignment_fails_closed_with_no_upstream_call(harness: Harness) -> None:
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True), clock=lambda: long_ago)
    _assert_refused_and_nothing_happened(harness, terminal)


def test_a_tampered_assignment_fails_closed_with_no_upstream_call(harness: Harness) -> None:
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True), tamper=True)
    _assert_refused_and_nothing_happened(harness, terminal)


def test_an_assignment_signed_with_a_key_serve_does_not_hold_fails_closed(harness: Harness) -> None:
    assert bytes(range(1, 33)) != ASSIGNMENT_KEY
    terminal = harness.run(
        body_for(ROUTE, tools=TOOLS, stream=True), key_provider=lambda: bytes(range(1, 33))
    )
    _assert_refused_and_nothing_happened(harness, terminal)


def test_an_assignment_dated_ahead_of_serves_clock_fails_closed(harness: Harness) -> None:
    ahead = datetime.now(timezone.utc) + timedelta(minutes=10)
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True), clock=lambda: ahead)
    _assert_refused_and_nothing_happened(harness, terminal)


def test_the_worker_sends_the_assignment_and_not_the_serve_token(harness: Harness) -> None:
    """The bearer the worker puts on its request is the signed assignment; the serve token (which
    the orchestrator still holds, for its readiness check) never leaves this process."""
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True))
    assert terminal.ok
    sent = harness.worker_saw()["token"]
    assert sent.startswith("vwa1.") and sent != ASSIGNMENT_SERVE_TOKEN
    assert ASSIGNMENT_SERVE_TOKEN not in json.dumps(harness.worker_saw())
