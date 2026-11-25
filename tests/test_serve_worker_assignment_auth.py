"""Serve authentication of a signed worker assignment (serve-worker-assignment, group 2).

A worker's only credential is ``Authorization: Bearer vwa1.<payload>.<tag>``.  These tests drive
``caller_authentication`` through ``TestClient`` with a recording fake upstream and a temp home
that holds the signing key.  Every denial asserts the same response as any wrong bearer, that the
relay handler was never reached, and that the upstream saw no request.  No live call is made and
the real home is never read: ``HOME`` points at a temp directory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

import verdict.api as api
from tests import worker_assignment_helpers as sign
from tests.test_proxy import FixedIntelligence, RecordingTransport
from verdict import worker_assignment as wa
from verdict.proxy import UpstreamProxy

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
SERVE_TOKEN = "serve-token-0123456789"
RELAY_PATHS = ["/v1/chat/completions", "/v1/responses"]
BODY: dict[str, Any] = {
    "model": "omniroute/free-route",
    "messages": [{"role": "user", "content": "do the task"}],
}
GENERIC = "authentication failed"


def _now() -> int:
    return int(time.time())


def _valid(**fields: Any) -> str:
    fields.setdefault("exp", _now() + 600)
    return sign.assignment(KEY, route_id="omniroute/free-route", **fields)


def _bearer(token: str) -> dict[str, Any]:
    value = f"Bearer {token}"
    # httpx refuses to encode a non-ASCII header string; send the raw bytes a hostile client could.
    return {"authorization": value if value.isascii() else value.encode("utf-8")}


def _write_key(home: Path, text: str | None) -> None:
    path = wa.assignment_key_path(home)
    if text is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """The app under test: temp home, key file, recording upstream, recording relay handler."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    _write_key(tmp_path, sign.b64url(KEY))
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    transport = RecordingTransport()
    routed: list[str] = []
    reached: list[dict[str, Any]] = []

    class CountingIntelligence(FixedIntelligence):
        async def route(self, task: str, *args: Any, **kwargs: Any) -> Any:
            routed.append(task)
            raise AssertionError("a refused request must not be routed")

    monkeypatch.setattr(api, "_build_intelligence", lambda: CountingIntelligence())
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy(
            "http://upstream.test/v1", api_key="gateway-key", transport=transport
        ),
    )

    async def recorder(request: Request, *, surface: str) -> JSONResponse:
        reached.append(
            {"surface": surface, "assignment": getattr(request.state, "worker_assignment", None)}
        )
        return JSONResponse({"reached": surface})

    monkeypatch.setattr(api, "_relay_completion", recorder)
    return SimpleNamespace(transport=transport, routed=routed, reached=reached, home=tmp_path)


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", SERVE_TOKEN)
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    return _install(monkeypatch, tmp_path)


@pytest.fixture
def anonymous(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    return _install(monkeypatch, tmp_path)


def _wrong_bearer_response(client: TestClient, path: str) -> tuple[int, bytes]:
    response = client.post(path, json=BODY, headers=_bearer("not-the-serve-token"))
    return response.status_code, response.content


def _assert_nothing_happened(serve: SimpleNamespace) -> None:
    assert serve.reached == []  # the relay handler was never reached
    assert serve.routed == []  # nothing was routed, so no proof was minted
    assert serve.transport.requests == []  # the upstream was never contacted


def _denial_tokens() -> dict[str, str]:
    good = _valid()
    other_tag = sign.tag_part(
        sign.assignment(KEY, route_id="omniroute/other-route", exp=_now() + 9)
    )
    soon = _now() + 600
    return {
        "forged-tag": f"vwa1.{sign.payload_part(good)}.{other_tag}",
        "wrong-key": sign.assignment(OTHER_KEY, route_id="omniroute/free-route", exp=soon),
        "expired": _valid(exp=_now() - 5),
        "expires-this-second": _valid(exp=_now()),
        "route-tampered": sign.retag_with_changed(good, route_id="omniroute/other-route"),
        "run-tampered": sign.retag_with_changed(good, run_id="run-other"),
        "node-tampered": sign.retag_with_changed(good, node_id="node-other"),
        "iat-tampered": sign.retag_with_changed(good, iat=soon - 1),
        "exp-tampered": sign.retag_with_changed(good, exp=soon + 1),
        "truncated": good[:-5],
        "over-length": good + "A" * 3000,
        "two-parts": f"vwa1.{sign.payload_part(good)}",
        "prefix-only": "vwa1.",
        "non-ascii": "vwa1.é.é",
        "exp-one-past-the-bound": sign.assignment(
            KEY, route_id="omniroute/free-route", exp=253402300800, iat=253402300000
        ),
        "exp-2-63": sign.assignment(KEY, route_id="omniroute/free-route", exp=2**63, iat=2**63 - 9),
        "lifetime-1801": _valid(exp=soon, iat=soon - 1801),
        "dated-years-ahead": sign.assignment(
            KEY, route_id="omniroute/free-route", exp=253402300799, iat=253402300798
        ),
    }


@pytest.mark.parametrize("path", RELAY_PATHS)
@pytest.mark.parametrize("case", sorted(_denial_tokens()))
def test_a_bad_assignment_is_denied_exactly_like_a_wrong_bearer(
    serve: SimpleNamespace, path: str, case: str
) -> None:
    token = _denial_tokens()[case]
    with TestClient(api.app) as client:
        status, content = _wrong_bearer_response(client, path)
        assert status == 403
        response = client.post(path, json=BODY, headers=_bearer(token))
    assert response.status_code == 403
    assert response.content == content  # the caller learns nothing about which check failed
    assert response.json()["error"]["message"] == GENERIC
    assert token not in response.text
    _assert_nothing_happened(serve)


@pytest.mark.parametrize("path", RELAY_PATHS)
@pytest.mark.parametrize("content", [None, "", "   \n", "not base64url!", sign.b64url(bytes(16))])
def test_a_missing_empty_or_short_key_fails_closed_with_503_for_an_assignment_only(
    serve: SimpleNamespace, path: str, content: str | None
) -> None:
    _write_key(serve.home, content)
    with TestClient(api.app) as client:
        refused = client.post(path, json=BODY, headers=_bearer(_valid()))
        # A request with the serve token and no assignment is served as before.
        served = client.post(path, json=BODY, headers=_bearer(SERVE_TOKEN))
    assert refused.status_code == 503
    assert refused.json()["error"]["message"] == "worker assignments are not configured"
    assert served.status_code == 200
    assert len(serve.reached) == 1  # only the serve-token request reached the handler
    assert serve.reached[0]["assignment"] is None
    assert serve.routed == []
    assert serve.transport.requests == []


@pytest.mark.parametrize("path", RELAY_PATHS)
def test_the_key_file_is_read_off_the_event_loop_on_every_assignment_request(
    serve: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A slow home filesystem must stall only the request that waits on it, not the loop."""
    real_loader = wa.load_assignment_key
    calls: list[bool] = []

    def spy(home: Path | None = None) -> bytes:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(False)  # a worker thread: no loop runs here
        else:
            calls.append(True)  # the caller is the event loop itself
        return real_loader(home)

    monkeypatch.setattr(wa, "load_assignment_key", spy)
    with TestClient(api.app) as client:
        for _ in range(2):
            response = client.post(path, json=BODY, headers=_bearer(_valid()))
            assert response.status_code == 200
    # One read per request (a rotation needs no restart), none of them on the event loop.
    assert calls == [False, False]


@pytest.mark.parametrize("path", RELAY_PATHS)
def test_a_key_loader_that_raises_off_the_loop_still_denies_with_no_upstream_call(
    serve: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def broken(home: Path | None = None) -> bytes:
        raise wa.AssignmentError(wa.REASON_KEY_UNAVAILABLE)

    monkeypatch.setattr(wa, "load_assignment_key", broken)
    with TestClient(api.app) as client:
        response = client.post(path, json=BODY, headers=_bearer(_valid()))
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "worker assignments are not configured"
    _assert_nothing_happened(serve)


@pytest.mark.parametrize(
    ("path", "surface"), [("/v1/chat/completions", "chat"), ("/v1/responses", "responses")]
)
def test_a_valid_assignment_reaches_the_handler_as_the_verified_type(
    serve: SimpleNamespace, path: str, surface: str
) -> None:
    token = _valid(run_id="run-7", node_id="node-9")
    with TestClient(api.app) as client:
        response = client.post(path, json=BODY, headers=_bearer(token))
    assert response.status_code == 200
    assert len(serve.reached) == 1
    assert serve.reached[0]["surface"] == surface
    verified = serve.reached[0]["assignment"]
    assert isinstance(verified, wa.VerifiedWorkerAssignment)
    assert (verified.route_id, verified.run_id, verified.node_id) == (
        "omniroute/free-route",
        "run-7",
        "node-9",
    )
    assert serve.transport.requests == []  # the handler here is the recorder, not the relay


def test_an_assignment_that_expires_mid_attempt_is_denied_on_its_next_request(
    serve: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is checked on every request: a late request of an attempt that was admitted fails."""
    from datetime import datetime as real_datetime
    from datetime import timedelta, timezone

    issued = real_datetime.now(timezone.utc)
    offset = {"seconds": 0}

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return real_datetime.now(tz) + timedelta(seconds=offset["seconds"])

    monkeypatch.setattr(api, "datetime", Clock)
    now = int(issued.timestamp())
    token = sign.assignment(KEY, route_id="omniroute/free-route", exp=now + 120, iat=now)
    with TestClient(api.app) as client:
        early = client.post("/v1/chat/completions", json=BODY, headers=_bearer(token))
        offset["seconds"] = 60  # still inside its lifetime
        middle = client.post("/v1/chat/completions", json=BODY, headers=_bearer(token))
        offset["seconds"] = 121  # the attempt outlived its assignment
        late = client.post("/v1/chat/completions", json=BODY, headers=_bearer(token))
    assert (early.status_code, middle.status_code) == (200, 200)
    assert late.status_code == 403
    assert late.json()["error"]["message"] == GENERIC
    assert len(serve.reached) == 2  # the late request never reached the handler
    assert serve.transport.requests == [] and serve.routed == []


def test_a_serve_token_request_carries_no_assignment(serve: SimpleNamespace) -> None:
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=BODY,
            headers={**_bearer(SERVE_TOKEN), "x-verdict-worker": "opencode"},
        )
    assert response.status_code == 200
    assert serve.reached[0]["assignment"] is None  # the header grants nothing at this layer


def test_the_key_is_read_again_for_every_request(serve: SimpleNamespace) -> None:
    token = _valid()
    with TestClient(api.app) as client:
        assert (
            client.post("/v1/chat/completions", json=BODY, headers=_bearer(token)).status_code
            == 200
        )
        _write_key(serve.home, sign.b64url(OTHER_KEY))  # rotation: the file is replaced
        assert (
            client.post("/v1/chat/completions", json=BODY, headers=_bearer(token)).status_code
            == 403
        )
        rotated = sign.assignment(OTHER_KEY, route_id="omniroute/free-route", exp=_now() + 600)
        assert (
            client.post("/v1/chat/completions", json=BODY, headers=_bearer(rotated)).status_code
            == 200
        )


OFF_PATH_REQUESTS = [
    pytest.param("GET", "/v1/guidance/status", None, id="guidance-status"),
    pytest.param("POST", "/v1/route", {"task": "x"}, id="route"),
    pytest.param("GET", "/v1/runs", None, id="runs"),
    pytest.param("GET", "/v1/runs/run-1/events", None, id="run-events"),
    pytest.param("GET", "/v1/models", None, id="models"),
    pytest.param(
        "POST", "/v1/messages", {"model": "m", "max_tokens": 1, "messages": []}, id="messages"
    ),
    pytest.param("GET", "/v1/chat/completions", None, id="relay-path-wrong-method"),
    pytest.param("PUT", "/v1/responses", {}, id="relay-path-put"),
]


@pytest.mark.parametrize(("method", "path", "body"), OFF_PATH_REQUESTS)
def test_a_valid_assignment_is_refused_off_the_relay_surfaces(
    serve: SimpleNamespace, method: str, path: str, body: Any
) -> None:
    with TestClient(api.app) as client:
        response = client.request(method, path, json=body, headers=_bearer(_valid()))
    assert response.status_code == 403
    assert response.json()["error"]["message"] == GENERIC
    _assert_nothing_happened(serve)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"x-api-key": "vwa1.\u00e9.\u00e9"}, id="x-api-key-alone"),
        pytest.param(
            {"x-api-key": "vwa1.\u00e9.\u00e9", "authorization": f"Bearer {SERVE_TOKEN}"},
            id="x-api-key-with-the-serve-bearer",
        ),
        pytest.param(
            {"x-api-key": "plain-ascii", "authorization": "Bearer vwa1.\u00e9.\u00e9"},
            id="bearer-with-an-ascii-x-api-key",
        ),
        pytest.param(
            {"x-api-key": "vwa1.\u00e9.\u00e9", "authorization": "Bearer vwa1.\u00e9.\u00e9"},
            id="both-non-ascii",
        ),
    ],
)
def test_a_non_ascii_assignment_credential_in_either_header_is_a_403_never_a_500(
    serve: SimpleNamespace, headers: dict[str, str]
) -> None:
    """The constant-time compare raises on non-ASCII text, so the prefix is checked first."""
    raw = {k: (v if v.isascii() else v.encode("utf-8")) for k, v in headers.items()}
    body = {"model": "m", "max_tokens": 1, "messages": []}
    with TestClient(api.app) as client:
        response = client.post("/v1/messages", json=body, headers=raw)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == GENERIC
    _assert_nothing_happened(serve)


def test_an_assignment_in_x_api_key_is_refused_on_messages(serve: SimpleNamespace) -> None:
    body = {"model": "m", "max_tokens": 1, "messages": []}
    token = _valid()
    with TestClient(api.app) as client:
        alone = client.post("/v1/messages", json=body, headers={"x-api-key": token})
        both = client.post(
            "/v1/messages", json=body, headers={"x-api-key": token, **_bearer(token)}
        )
        with_serve_bearer = client.post(
            "/v1/messages", json=body, headers={"x-api-key": token, **_bearer(SERVE_TOKEN)}
        )
    for response in (alone, both, with_serve_bearer):
        assert response.status_code == 403
        assert response.json()["error"]["message"] == GENERIC
    _assert_nothing_happened(serve)


def test_health_ignores_the_bearer_and_still_answers(serve: SimpleNamespace) -> None:
    with TestClient(api.app) as client:
        response = client.get("/health", headers=_bearer(_valid()))
    assert response.status_code == 200
    assert serve.reached == []


def test_the_serve_token_still_authenticates_every_other_path(serve: SimpleNamespace) -> None:
    with TestClient(api.app) as client:
        status = client.get("/v1/guidance/status", headers=_bearer(SERVE_TOKEN))
        missing = client.get("/v1/guidance/status")
        wrong = client.get("/v1/guidance/status", headers=_bearer("nope"))
    assert status.status_code == 200
    assert missing.status_code == 401
    assert wrong.status_code == 403


def test_a_failed_verification_logs_the_reason_code_and_nothing_from_the_token(
    serve: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    forged = sign.assignment(
        OTHER_KEY, route_id="omniroute/secret-route", run_id="run-secret", exp=_now() + 600
    )
    with caplog.at_level(logging.DEBUG), TestClient(api.app) as client:
        response = client.post("/v1/chat/completions", json=BODY, headers=_bearer(forged))
    assert response.status_code == 403
    logged = caplog.text
    assert "bad_signature" in logged
    for secret in (
        forged,
        sign.payload_part(forged),
        sign.tag_part(forged),
        "omniroute/secret-route",
        "run-secret",
        sign.b64url(KEY),
    ):
        assert secret not in logged
        assert secret not in response.text


def test_a_valid_assignment_is_never_logged(
    serve: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    token = _valid()
    with caplog.at_level(logging.DEBUG), TestClient(api.app) as client:
        client.post("/v1/chat/completions", json=BODY, headers=_bearer(token))
    assert token not in caplog.text
    assert sign.tag_part(token) not in caplog.text


# --- anonymous mode ---------------------------------------------------------------------


@pytest.fixture
def untouched_key(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spies that fail the test if anonymous mode reads the key or verifies anything."""
    calls: list[str] = []

    def boom(name: str) -> Any:
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} must not run in anonymous mode")

        return _fail

    monkeypatch.setattr(wa, "load_assignment_key", boom("load_assignment_key"))
    monkeypatch.setattr(wa, "verify_assignment", boom("verify_assignment"))
    return calls


ANON_REQUESTS = [
    pytest.param("POST", "/v1/chat/completions", BODY, id="chat"),
    pytest.param("POST", "/v1/responses", BODY, id="responses"),
    pytest.param("GET", "/v1/guidance/status", None, id="guidance-status"),
    pytest.param("POST", "/v1/route", {"task": "x"}, id="route"),
    pytest.param("GET", "/v1/runs", None, id="runs"),
    pytest.param("GET", "/v1/models", None, id="models"),
    pytest.param(
        "POST", "/v1/messages", {"model": "m", "max_tokens": 1, "messages": []}, id="messages"
    ),
]


@pytest.mark.parametrize("valid", [True, False], ids=["valid-assignment", "forged-assignment"])
@pytest.mark.parametrize(("method", "path", "body"), ANON_REQUESTS)
def test_anonymous_mode_refuses_an_assignment_bearer_on_every_path(
    anonymous: SimpleNamespace,
    untouched_key: list[str],
    method: str,
    path: str,
    body: Any,
    valid: bool,
) -> None:
    token = (
        _valid()
        if valid
        else sign.assignment(OTHER_KEY, route_id="omniroute/free-route", exp=_now() + 600)
    )
    with TestClient(api.app) as client:
        response = client.request(method, path, json=body, headers=_bearer(token))
    assert response.status_code == 403
    assert response.json()["error"]["message"] == GENERIC
    assert untouched_key == []  # nothing verified, the key file never read
    _assert_nothing_happened(anonymous)


@pytest.mark.parametrize(("method", "path", "body"), ANON_REQUESTS)
def test_anonymous_mode_refuses_the_assignment_prefix_in_x_api_key(
    anonymous: SimpleNamespace, untouched_key: list[str], method: str, path: str, body: Any
) -> None:
    with TestClient(api.app) as client:
        response = client.request(method, path, json=body, headers={"x-api-key": _valid()})
    assert response.status_code == 403
    assert response.json()["error"]["message"] == GENERIC
    assert untouched_key == []
    _assert_nothing_happened(anonymous)


def test_anonymous_mode_without_an_assignment_is_unchanged(
    anonymous: SimpleNamespace, untouched_key: list[str]
) -> None:
    with TestClient(api.app) as client:
        status = client.get("/v1/guidance/status")
        relay = client.post("/v1/chat/completions", json=BODY)
        other_bearer = client.post(
            "/v1/chat/completions", json=BODY, headers=_bearer("anything-else")
        )
    assert status.status_code == 200
    assert relay.status_code == 200
    assert other_bearer.status_code == 200  # anonymous mode ignores a bearer it does not know
    assert len(anonymous.reached) == 2
    assert all(row["assignment"] is None for row in anonymous.reached)
    assert untouched_key == []


def test_anonymous_mode_still_refuses_a_non_loopback_client(
    anonymous: SimpleNamespace, untouched_key: list[str]
) -> None:
    with TestClient(api.app, client=("203.0.113.9", 4000)) as remote:
        plain = remote.get("/v1/guidance/status")
        with_assignment = remote.post("/v1/chat/completions", json=BODY, headers=_bearer(_valid()))
    for response in (plain, with_assignment):
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "anonymous mode is loopback-only"
    assert untouched_key == []
    _assert_nothing_happened(anonymous)


def test_anonymous_mode_with_the_worker_header_alone_reaches_the_relay_guard(
    anonymous: SimpleNamespace,
) -> None:
    # The middleware does not decide this case (no assignment is presented); the relay guard
    # does, and is tested with the real relay in tests/test_serve_route_pinning.py.
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions", json=BODY, headers={"x-verdict-worker": "opencode"}
        )
    assert response.status_code == 200
    assert anonymous.reached[0]["assignment"] is None


def test_no_token_and_no_anonymous_mode_is_503_for_an_assignment_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    state = _install(monkeypatch, tmp_path)
    # Startup itself refuses this configuration, so the middleware runs without the lifespan.
    response = TestClient(api.app).post(
        "/v1/chat/completions", json=BODY, headers=_bearer(_valid())
    )
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "server authentication is not configured"
    _assert_nothing_happened(state)
