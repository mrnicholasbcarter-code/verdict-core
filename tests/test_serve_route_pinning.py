"""The relay honours a worker's signed assignment, and the bare header selects nothing.

ADR-053 decision 1, as amended by the serve-worker-assignment change: a worker's route comes
only from a verified assignment (``Authorization: Bearer vwa1.<payload>.<tag>``).  The
``X-Verdict-Worker`` header alone, with the serve token or with nothing, pins no route and is
refused 403 before routing.  For a verified assignment the request's ``model`` must equal the
signed route (a pre-routing 400 otherwise), and after routing the relay bounds the request to
that one route with no alternatives.

Every refusal asserts zero upstream calls and that the routing double was never reached.  The
upstream is a RecordingTransport; nothing here makes a live call or reads the real home.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import verdict.api as api
from tests.relay_proof_helpers import (
    ASSIGNMENT_SERVE_TOKEN,
    assignment_headers,
    assignment_token,
    configure_assignment_auth,
    serve_token_headers,
    with_relay_proofs,
)
from tests.test_proxy import FixedIntelligence, RecordingTransport
from verdict.models import RoutingDecision
from verdict.proxy import UpstreamProxy

SURFACES = ["/v1/chat/completions", "/v1/responses"]
ASSIGNED = "selected-model"
WORKER = {"x-verdict-worker": "opencode"}


class _WorkerPinningIntelligence(FixedIntelligence):
    """Intelligence that returns a decision with two admitted routes.

    The primary model is ``other-primary``; ``selected-model`` is an admitted alternative.
    ``RecordingTransport`` hardcodes ``"model":"selected-model"`` in its response body, so
    ``other-primary`` as the upstream request model would trigger the relay's model-identity
    check and produce a 503: the positive tests are discriminating.  It counts every routing.
    """

    def __init__(self) -> None:
        self.routed: list[dict[str, Any] | None] = []

    async def route(
        self,
        task: str,
        criticality: str = "medium",
        context: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> RoutingDecision:
        self.routed.append(context)
        decision = RoutingDecision(
            model=self._selected(),
            provider="omniroute",
            tier=2,
            reason="worker pinning test fixture",
            request_id="pin-req-1",
            managed_backend_status="healthy",
            quality_outcome="unknown",
            alternatives=self._alternatives(),
            candidate_states=[
                {"model_id": "other-primary", "admitted": True},
                {"model_id": "selected-model", "admitted": True},
            ],
        )
        return with_relay_proofs(decision, context)

    def _selected(self) -> str:
        return "other-primary"

    def _alternatives(self) -> list[str]:
        return ["selected-model"]


class _SelectsAssignedRoute(_WorkerPinningIntelligence):
    def _selected(self) -> str:
        return "selected-model"

    def _alternatives(self) -> list[str]:
        return []


class _DecisionWithAlternatives(_WorkerPinningIntelligence):
    """Selects the assigned route and also lists an admitted alternative with its own proof.

    No accepted worker request produces such a decision, so this is defence in depth: the relay
    bounds an accepted worker request to its one route whatever the decision carries.
    """

    def _selected(self) -> str:
        return "selected-model"

    def _alternatives(self) -> list[str]:
        return ["other-primary"]


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    intelligence: _WorkerPinningIntelligence | None = None,
) -> tuple[RecordingTransport, _WorkerPinningIntelligence]:
    """The test app: token mode, key in a temp home, routing double, recording upstream."""
    transport = RecordingTransport()
    service = intelligence or _SelectsAssignedRoute()
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy(
            "http://upstream.test/v1", api_key="gateway-key", transport=transport
        ),
    )
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    configure_assignment_auth(monkeypatch, tmp_path)
    return transport, service


def _body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": ASSIGNED,
        "messages": [{"role": "user", "content": "worker task"}],
    }
    body.update(extra)
    return body


# --- the bare header selects nothing ---------------------------------------------------------


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("header", ["opencode", "", "anything"])
def test_the_serve_token_with_the_worker_header_pins_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str, header: str
) -> None:
    transport, service = _configure(monkeypatch, tmp_path, _WorkerPinningIntelligence())
    with TestClient(api.app) as client:
        response = client.post(
            surface,
            json=_body(model="selected-model"),  # an admitted route, named by the worker
            headers={**serve_token_headers(), "x-verdict-worker": header},
        )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == "authentication failed"
    assert service.routed == []  # nothing was routed, so nothing was pinned or proved
    assert transport.requests == []


@pytest.mark.parametrize("surface", SURFACES)
def test_the_worker_header_with_no_valid_authentication_is_refused_like_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    transport, service = _configure(monkeypatch, tmp_path)
    with TestClient(api.app) as client:
        missing = client.post(surface, json=_body(), headers=WORKER)
        wrong = client.post(
            surface, json=_body(), headers={**WORKER, "authorization": "Bearer not-the-token"}
        )
    assert missing.status_code == 401
    assert wrong.status_code == 403
    assert service.routed == []
    assert transport.requests == []


@pytest.mark.parametrize("surface", SURFACES)
def test_anonymous_mode_with_the_worker_header_alone_is_denied_by_the_relay_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    transport, service = _configure(monkeypatch, tmp_path)
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN")
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    with TestClient(api.app) as client:
        response = client.post(surface, json=_body(), headers=WORKER)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["message"] == "authentication failed"
        assert service.routed == []
        assert transport.requests == []
        # The same request without the header is anonymous traffic and is served as before.
        unmarked = client.post(surface, json=_body())
    assert unmarked.status_code == 200, unmarked.text
    assert len(service.routed) == 1
    assert [r["body"]["model"] for r in transport.requests] == ["selected-model"]


def _direct_request(headers: dict[str, str], body: bytes) -> Request:
    """A request built by hand, so the authentication middleware is out of the way."""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "client": ("127.0.0.1", 50000),
        "state": {},
    }
    return Request(scope, receive)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"authorization": "Bearer vwa1.e30.e30"}, id="assignment-bearer-unverified"),
        pytest.param(WORKER, id="worker-header-alone"),
        pytest.param({**WORKER, "authorization": "Bearer vwa1.e30.e30"}, id="both"),
    ],
)
def test_the_relay_guard_denies_a_worker_marked_request_without_a_verified_assignment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, headers: dict[str, str]
) -> None:
    """The second line behind the middleware: called with the middleware out of the way."""
    transport, service = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "intelligence_instance", service)
    monkeypatch.setattr(
        api,
        "proxy_instance",
        UpstreamProxy("http://upstream.test/v1", api_key="gateway-key", transport=transport),
    )
    import json

    request = _direct_request(headers, json.dumps(_body()).encode())
    assert not hasattr(request.state, "worker_assignment")
    response = asyncio.run(api._relay_completion(request, surface="chat"))
    assert response.status_code == 403
    assert service.routed == []
    assert transport.requests == []


def test_a_request_with_no_assignment_and_no_marker_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Its ``model`` does not select the route: admission's own pick is served."""
    transport, service = _configure(monkeypatch, tmp_path)  # admission selects selected-model
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_body(model="other-primary"),  # the client names another admitted route
            headers=serve_token_headers(),
        )
    assert response.status_code == 200, response.text
    assert [r["body"]["model"] for r in transport.requests] == ["selected-model"]
    assert "_worker_assignment" not in (service.routed[0] or {})


# --- a verified assignment assigns, and its model must match -----------------------------------


@pytest.mark.parametrize("surface", SURFACES)
def test_a_verified_assignment_needs_no_header_and_reaches_routing_with_the_typed_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    from verdict.models import CONTEXT_WORKER_ASSIGNMENT
    from verdict.worker_assignment import VerifiedWorkerAssignment

    transport, service = _configure(monkeypatch, tmp_path)
    with TestClient(api.app) as client:
        response = client.post(
            surface,
            json=_body(input="worker task"),
            headers=assignment_headers(ASSIGNED, run_id="run-7", node_id="node-9"),
        )
    assert response.status_code == 200, response.text
    assert [r["body"]["model"] for r in transport.requests] == [ASSIGNED]
    assigned = service.routed[0][CONTEXT_WORKER_ASSIGNMENT]  # type: ignore[index]
    assert isinstance(assigned, VerifiedWorkerAssignment)
    assert (assigned.route_id, assigned.run_id, assigned.node_id) == (ASSIGNED, "run-7", "node-9")


@pytest.mark.parametrize("surface", SURFACES)
def test_a_client_cannot_supply_the_assignment_context_key_in_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    _transport, service = _configure(monkeypatch, tmp_path, _WorkerPinningIntelligence())
    with TestClient(api.app) as client:
        client.post(
            surface,
            json=_body(_worker_assignment={"route_id": ASSIGNED}, worker_assignment=ASSIGNED),
            headers=serve_token_headers(),
        )
    assert len(service.routed) == 1
    assert "_worker_assignment" not in (service.routed[0] or {})


_OMIT = object()
MODEL_CASES = [
    pytest.param("other-primary", id="another-admitted-route"),
    pytest.param("foreign-model", id="unknown-route"),
    pytest.param("SELECTED-MODEL", id="case-differs"),
    pytest.param(" selected-model", id="leading-space"),
    pytest.param("selected-model ", id="trailing-space"),
    pytest.param(_OMIT, id="omitted"),
    pytest.param(None, id="null"),
    pytest.param("", id="empty"),
    pytest.param(123, id="number"),
    pytest.param(["selected-model"], id="list"),
    pytest.param({"id": "selected-model"}, id="object"),
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("model", MODEL_CASES)
def test_a_model_that_differs_from_the_signed_route_is_refused_before_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    surface: str,
    model: object,
) -> None:
    transport, service = _configure(monkeypatch, tmp_path)
    body = _body()
    if model is _OMIT:
        del body["model"]
    else:
        body["model"] = model
    headers = assignment_headers(ASSIGNED, run_id="run-42", node_id="node-17")
    token = headers["authorization"].split(" ", 1)[1]
    with caplog.at_level(logging.DEBUG), TestClient(api.app) as client:
        response = client.post(surface, json=body, headers=headers)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert service.routed == [], "the ladder ran for a request that contradicts its credential"
    assert transport.requests == []
    # The refusal is logged with the reason code and the signed route, run and node ids...
    logged = caplog.text
    assert "model_mismatch" in logged
    assert ASSIGNED in logged and "run-42" in logged and "node-17" in logged
    # ...and never the token or any part of it.
    assert token not in logged and token not in response.text
    assert token.split(".")[2] not in logged


@pytest.mark.parametrize("surface", SURFACES)
def test_a_decision_for_another_route_is_denied_and_nothing_is_dispatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str
) -> None:
    """The double ignores the assignment and selects ``other-primary``: the relay denies it."""
    transport, service = _configure(monkeypatch, tmp_path, _WorkerPinningIntelligence())
    with TestClient(api.app) as client:
        response = client.post(surface, json=_body(), headers=assignment_headers(ASSIGNED))
    assert response.status_code == 503, response.text
    assert len(service.routed) == 1  # it was routed; the relay then refused the outcome
    assert transport.requests == []
    assert "not selected by admission" in response.json()["error"]["message"]


def test_the_matching_model_is_served_on_exactly_one_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A decision that lists an admitted alternative still yields one attempt, no fallback."""
    transport, _ = _configure(monkeypatch, tmp_path, _DecisionWithAlternatives())
    attempted: list[list[str]] = []
    real_build_attempts = api.build_attempts

    def recording(*args: Any, **kwargs: Any) -> Any:
        attempts = real_build_attempts(*args, **kwargs)
        attempted.append([item.model for item in attempts])
        return attempts

    monkeypatch.setattr(api, "build_attempts", recording)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions", json=_body(), headers=assignment_headers(ASSIGNED)
        )
    assert response.status_code == 200, response.text
    assert attempted == [[ASSIGNED]], f"fallback attempts were built: {attempted}"
    assert [r["body"]["model"] for r in transport.requests] == [ASSIGNED]


# --- execution_path_request -----------------------------------------------------------------


_EXECUTION_PATH_VALUES: list[Any] = [{"schema_version": "anything"}, {}, None, "not-an-object"]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("value", _EXECUTION_PATH_VALUES, ids=["object", "empty", "null", "string"])
def test_an_assigned_request_carrying_execution_path_request_is_rejected_before_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, surface: str, value: Any
) -> None:
    """Whatever the field holds (present, even null, is enough) and on either relay surface.

    The execution-path flow selects its own offer and would bypass the assigned route, and
    pinned execution-path support for workers is not built (operator decision, 2026-11-24).
    """
    transport, service = _configure(monkeypatch, tmp_path)
    with TestClient(api.app) as client:
        response = client.post(
            surface,
            json=_body(input="worker task", execution_path_request=value),
            headers=assignment_headers(ASSIGNED),
        )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "execution_path_request" in message and "worker" in message
    assert service.routed == [], "an assigned request with execution_path_request reached routing"
    assert transport.requests == []


def test_the_execution_path_rejection_is_only_for_assigned_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an assignment the request is judged as before: the existing validation error."""
    _configure(monkeypatch, tmp_path)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=_body(execution_path_request={"schema_version": "anything"}),
            headers=serve_token_headers(),
        )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "schema_version" in message, message
    assert "worker" not in message, message


def test_the_assignment_token_never_reaches_the_upstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport, _ = _configure(monkeypatch, tmp_path)
    token = assignment_token(ASSIGNED)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions", json=_body(), headers={"authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200, response.text
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert token not in str(sent["headers"]) and token not in str(sent["body"])
    assert ASSIGNMENT_SERVE_TOKEN not in str(sent["headers"])
    assert sent["headers"]["authorization"] == "Bearer gateway-key"
