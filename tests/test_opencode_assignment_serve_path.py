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
* an expired, foreign-key or future-dated assignment is refused by the pre-launch probe (#1340):
  the first attempt of a run ends as a configuration block before anything is launched, with no
  upstream call, no routing decision, no proof and no evidence, and without reading serve's
  receipts (so a remote or spare serve is covered); a serve with no signing key is the same block;
* a healthy serve answers the probe 400 (its model check, after it verified the assignment and
  before any routing), so the probe decides nothing and sends nothing upstream;
* a refusal after the probe (a tampered copy, a key rotated since) is serve's own 403 to the
  worker: the attempt ends as a configuration block rather than a provider denial once serve's
  receipt database has been seen to hold a request of this executor's own assignments (an empty
  read of a database nothing has tied to the selected serve proves nothing, so a first refusal
  after the probe stays the provider's and its receipts are unavailable).
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
import verdict.orchestration.executor_opencode as executor_module
from tests import worker_assignment_helpers as sign
from tests.relay_proof_helpers import ASSIGNMENT_KEY, ASSIGNMENT_SERVE_TOKEN, assignment_token
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
from verdict.worker_assignment import assignment_key_path

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
if os.path.exists({tamper_flag!r}):
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
        self.tamper_flag = tmp_path / "tamper-the-assignment"
        self.receipts_asked = 0  # how often the executor looked for serve's receipt database

    def worker(self, body: dict[str, Any]) -> str:
        script = self.tmp_path / "fake-opencode"
        script.write_text(
            "#!/usr/bin/env python3\n"
            + textwrap.dedent(WORKER).format(
                url=self.origin, body=body, out=str(self.out), tamper_flag=str(self.tamper_flag)
            )
        )
        script.chmod(0o755)
        return str(script)

    def _receipts_db(self) -> Path:
        self.receipts_asked += 1
        return self.db

    def executor(self, body: dict[str, Any], **overrides: Any) -> OpenCodeExecutor:
        args: dict[str, Any] = {
            "opencode_bin": self.worker(body),
            "serve_base_url": self.origin,
            "receipts_db_provider": self._receipts_db,
        }
        args.update(overrides)
        return OpenCodeExecutor(**args)

    def attempt(self, executor: OpenCodeExecutor, *, tamper: bool = False) -> Any:
        """One attempt on ``executor`` (reuse one to carry what it has seen of serve's database)."""
        if tamper:
            self.tamper_flag.write_text("1")
        else:
            self.tamper_flag.unlink(missing_ok=True)
        cwd = self.tmp_path / "work"
        cwd.mkdir(exist_ok=True)
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

    def run(self, body: dict[str, Any], *, tamper: bool = False, **overrides: Any) -> Any:
        return self.attempt(self.executor(body, **overrides), tamper=tamper)

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
    # the pre-launch probe (#1340) decided and minted nothing: the worker's request is the only one
    assert len(harness.wired.decisions) == 1 and len(harness.wired.minted) == 1
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
    """What does not depend on whether the executor can say who refused: serve answered 403
    ``authentication failed`` to the worker, nothing was sent upstream, nothing was recorded."""
    seen = harness.worker_saw()
    assert seen["status"] == 403
    assert json.loads(seen["body"])["error"]["message"] == "authentication failed"
    assert harness.wired.upstream == [], "a refused assignment reached the upstream"
    assert harness.evidence_roots() == [], "serve wrote evidence for a refused assignment"
    assert terminal.ok is False
    assert terminal.status_code == 403


def _assert_provider_denial_for_want_of_proof(terminal: Any) -> None:
    """No request of this executor's has been seen in serve's database yet, so the empty read
    does not show that serve refused: the failure stays the provider's, its receipts unavailable."""
    assert not terminal.error.startswith("harness_configuration:"), terminal.error
    serve = terminal.worker_assignment["serve_receipts"]
    assert serve["status"] == "unavailable"
    assert (serve["requests"], serve["requests_waived"]) == (None, None)
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert verdict.category != "harness_configuration"


def _assert_configuration_block(terminal: Any) -> None:
    assert terminal.error.startswith("harness_configuration: verdict serve refused"), terminal.error
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action, verdict.cooldown_seconds) == (
        "harness_configuration",
        "BLOCK",
        0,
    )
    serve = terminal.worker_assignment["serve_receipts"]
    assert (serve["status"], serve["requests"], serve["requests_waived"]) == ("read", 0, 0)


def _assert_blocked_before_launch(harness: Harness, terminal: Any) -> None:
    """The first attempt ended as a configuration block before anything was launched: serve
    refused the attempt's own assignment to the probe, so no worker ran, nothing was routed,
    proven or sent upstream, serve wrote no evidence, and recovery blocks without a cooldown."""
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: verdict serve at "), terminal.error
    assert not harness.out.exists(), "a worker was launched"
    assert terminal.worker_assignment is None
    assert terminal.status_code is None
    assert harness.wired.upstream == [], "a refused assignment reached the upstream"
    assert harness.wired.decisions == [], "the probe made a routing decision"
    assert harness.wired.minted == [], "the probe minted an admission proof"
    assert harness.evidence_roots() == [], "serve wrote evidence for a refused assignment"
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action, verdict.cooldown_seconds, verdict.scope) == (
        "harness_configuration",
        "BLOCK",
        0,
        "none",
    )


def _foreign_key() -> bytes:
    other = bytes(range(1, 33))
    assert other != ASSIGNMENT_KEY
    return other


@pytest.mark.parametrize("fault", ["expired", "foreign-key", "dated-ahead"])
def test_a_first_attempt_serve_refuses_is_a_configuration_block_before_anything_is_launched(
    harness: Harness, fault: str
) -> None:
    """The very first assignment of a run, signed with a key serve does not hold, expired, or
    dated ahead of serve's clock: serve answers the probe with its refusal, so the attempt ends
    as a block naming the possible causes and the fix, with no worker launched, nothing sent
    upstream and no provider cooled down."""
    overrides: dict[str, Any] = {}
    if fault == "expired":
        overrides["clock"] = lambda: datetime.now(timezone.utc) - timedelta(hours=2)
    elif fault == "dated-ahead":
        overrides["clock"] = lambda: datetime.now(timezone.utc) + timedelta(minutes=10)
    else:
        overrides["key_provider"] = _foreign_key
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True), **overrides)
    _assert_blocked_before_launch(harness, terminal)
    for cause in (
        "worker-assignment.key",  # a different key
        "HOME",  # or home
        "60 s",  # clocks
        "older than the signed-assignment change",  # serve too old
        "anonymous",
        "verdict setup --apply --allow host.serve",  # the fix
    ):
        assert cause in terminal.error, (cause, terminal.error)
    assert harness.receipts_asked == 0, "the refusal needed serve's receipts"


def test_a_serve_whose_receipts_the_orchestrator_cannot_read_is_covered_by_the_probe(
    harness: Harness,
) -> None:
    """A remote or spare serve writes receipts the orchestrator cannot open.  Nothing about the
    refusal is read from them: the probe asked serve itself."""
    asked: list[str] = []

    def unreadable() -> Path:
        asked.append("asked")
        raise OSError("serve's receipt database is on another host")

    terminal = harness.run(
        body_for(ROUTE, tools=TOOLS, stream=True),
        key_provider=_foreign_key,
        receipts_db_provider=unreadable,
    )
    _assert_blocked_before_launch(harness, terminal)
    assert asked == [], "the receipts of a refused first attempt were looked for"


def test_a_serve_with_no_signing_key_is_the_same_block_naming_setup(harness: Harness) -> None:
    """Serve answers 503 'worker assignments are not configured' (its key file is missing or
    unusable): the block says so and names ``verdict setup``."""
    assignment_key_path(harness.tmp_path).unlink()
    terminal = harness.run(
        body_for(ROUTE, tools=TOOLS, stream=True), key_provider=lambda: ASSIGNMENT_KEY
    )
    _assert_blocked_before_launch(harness, terminal)
    assert "no worker assignment signing key" in terminal.error, terminal.error
    assert "verdict setup --apply --allow host.serve" in terminal.error


def test_a_healthy_serve_answers_the_probe_400_and_the_probe_decides_and_sends_nothing(
    harness: Harness,
) -> None:
    """Serve verified the attempt's assignment and refused the sentinel model with its model
    check, which comes after verification and before any routing: so 400 means accepted, and the
    probe made no decision, minted no proof, sent nothing upstream and wrote no evidence."""
    probe = executor_module._probe_assignment(harness.origin, assignment_token(ROUTE), 10.0)
    assert (probe.outcome, probe.status, probe.reason) == ("accepted", 400, None)
    assert harness.wired.upstream == []
    assert harness.wired.decisions == []
    assert harness.wired.minted == []
    assert harness.evidence_roots() == []


def test_the_probe_is_refused_by_the_real_serve_for_each_way_an_assignment_goes_wrong(
    harness: Harness,
) -> None:
    """The refusal serve gives the probe is the one the executor recognises, in the form serve
    really writes it (nothing is stubbed between the real app and the executor's reader)."""
    tampered = assignment_token(ROUTE)[:-2] + "AA"
    foreign = sign.assignment(
        _foreign_key(),
        route_id=ROUTE,
        run_id=RUN_ID,
        node_id=NODE_ID,
        exp=int(time.time()) + 300,
        iat=int(time.time()),
    )
    for token in (tampered, foreign):
        probe = executor_module._probe_assignment(harness.origin, token, 10.0)
        assert (probe.outcome, probe.status) == ("refused", 403)
        assert probe.reason is not None and "verdict serve at" in probe.reason
    assert harness.wired.upstream == [] and harness.wired.decisions == []


def test_a_tampered_assignment_fails_closed_with_no_upstream_call(harness: Harness) -> None:
    """The worker alters its own copy after the executor's probe passed: serve's refusal comes
    mid-run, from a first attempt, so nothing shows the database is serve's and it stays the
    provider's (the receipt-based classification is unchanged)."""
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True), tamper=True)
    _assert_refused_and_nothing_happened(harness, terminal)
    _assert_provider_denial_for_want_of_proof(terminal)


def _rotate_serves_key_after_the_probe(
    monkeypatch: pytest.MonkeyPatch, harness: Harness, armed: dict[str, bool]
) -> None:
    """After the executor's probe has passed (while ``armed``), serve's key file is replaced with
    another key: serve reads it on every request, so the worker's request is refused."""
    real = executor_module._check_assignment

    def probe_then_rotate(origin: str, assignment: str, timeout: float) -> str | None:
        reason = real(origin, assignment, timeout)
        if armed["on"]:
            path = assignment_key_path(harness.tmp_path)
            path.write_text(sign.b64url(_foreign_key()), encoding="utf-8")
        return reason

    monkeypatch.setattr(executor_module, "_check_assignment", probe_then_rotate)


def test_a_key_rotated_after_the_probe_is_a_refusal_mid_run_and_stays_the_providers_at_first(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rotate_serves_key_after_the_probe(monkeypatch, harness, {"on": True})
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True))
    _assert_refused_and_nothing_happened(harness, terminal)
    _assert_provider_denial_for_want_of_proof(terminal)


@pytest.mark.parametrize("fault", ["tampered", "key-rotated-after-the-probe"])
def test_a_refusal_after_serve_admitted_an_earlier_attempt_is_a_configuration_block(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Serve admitted this executor's first assignment, so its receipt database is shown to be the
    one the selected serve writes.  A later assignment that serve refuses to the worker, after the
    probe passed (nothing recorded under it), is then serve's refusal, whatever the reason, and
    ends the attempt as a block."""
    armed = {"on": False}
    _rotate_serves_key_after_the_probe(monkeypatch, harness, armed)
    body = body_for(ROUTE, tools=TOOLS, stream=True)
    ex = harness.executor(body, key_provider=lambda: ASSIGNMENT_KEY)
    first = harness.attempt(ex)
    assert first.ok, first
    assert first.worker_assignment["serve_receipts"]["requests"] == 1
    assert len(harness.wired.upstream) == 1

    armed["on"] = fault == "key-rotated-after-the-probe"
    second = harness.attempt(ex, tamper=fault == "tampered")

    assert harness.worker_saw()["status"] == 403
    assert len(harness.wired.upstream) == 1, "a refused assignment reached the upstream"
    assert len(harness.evidence_roots()) == 1, "serve wrote evidence for a refused assignment"
    assert second.ok is False
    assert second.status_code == 403
    _assert_configuration_block(second)


def test_the_worker_sends_the_assignment_and_not_the_serve_token(harness: Harness) -> None:
    """The bearer the worker puts on its request is the signed assignment; the serve token (which
    the orchestrator still holds, for its readiness check) never leaves this process."""
    terminal = harness.run(body_for(ROUTE, tools=TOOLS, stream=True))
    assert terminal.ok
    sent = harness.worker_saw()["token"]
    assert sent.startswith("vwa1.") and sent != ASSIGNMENT_SERVE_TOKEN
    assert ASSIGNMENT_SERVE_TOKEN not in json.dumps(harness.worker_saw())
