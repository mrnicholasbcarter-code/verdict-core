"""Route-pinning tests for the OpenCode worker adapter (ADR-053 Decision 1, Task 2.4).

These tests cover the relay's side of per-request route pinning for OpenCode workers:
when ``X-Verdict-Worker`` is present, the ``model`` field is the route the worker was
assigned.  The relay hands it to admission as a typed input; admission selects it within
its admitted set and mints its proof, or denies.  The relay verifies that outcome and
fails closed with 503 otherwise (invalid ``model``, route not admitted, or not selected).

No live calls are made.  The upstream is a RecordingTransport fixture.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests.relay_proof_helpers import with_relay_proofs
from tests.test_proxy import FixedIntelligence, RecordingTransport
from verdict.models import RoutingDecision
from verdict.proxy import UpstreamProxy

# ---------------------------------------------------------------------------
# Fixture intelligence with explicit admitted candidate_states
# ---------------------------------------------------------------------------


class _WorkerPinningIntelligence(FixedIntelligence):
    """Intelligence that returns a decision with two admitted routes.

    The primary model is ``other-primary``; ``selected-model`` is an admitted
    alternative.  Pinning to ``selected-model`` must override the primary
    selection.  ``RecordingTransport`` hardcodes ``"model":"selected-model"``
    in its response body, so ``other-primary`` as the upstream request model
    would trigger the relay's model-identity check and produce a 503 without
    pinning — making the positive test genuinely discriminating.
    """

    async def route(
        self,
        task: str,
        criticality: str = "medium",
        context: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> RoutingDecision:
        decision = RoutingDecision(
            model="other-primary",
            provider="omniroute",
            tier=2,
            reason="worker pinning test fixture",
            request_id="pin-req-1",
            managed_backend_status="healthy",
            quality_outcome="unknown",
            alternatives=["selected-model"],
            candidate_states=[
                {"model_id": "other-primary", "admitted": True},
                {"model_id": "selected-model", "admitted": True},
            ],
        )
        return with_relay_proofs(decision, context)


def _configure(monkeypatch: pytest.MonkeyPatch) -> RecordingTransport:
    """Wire up the test app with pinning intelligence and a recording transport."""
    transport = RecordingTransport()
    monkeypatch.setattr(api, "_build_intelligence", lambda: _WorkerPinningIntelligence())
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy("http://upstream.test/v1", api_key="test-key", transport=transport),
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    return transport


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_relay_never_rewrites_a_worker_decision_to_an_unselected_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay only verifies admission's outcome for a worker; it never rewrites it.

    This double ignores the worker's assigned route and selects ``other-primary``.  The
    assigned ``selected-model`` is admitted, but it was not selected (and so carries no
    proof from admission), so dispatching ``other-primary`` or swapping the model here
    would be wrong.  The request fails closed with no upstream contact.  The positive
    path, with production-shaped proofs, is in ``tests/test_serve_worker_route_proof.py``.
    """
    transport = _configure(monkeypatch)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "selected-model",
                "messages": [{"role": "user", "content": "worker task"}],
            },
            headers={"x-verdict-worker": "opencode"},
        )
    assert response.status_code == 503, response.text
    assert transport.requests == []
    assert "not selected by admission" in response.json()["error"]["message"]


def test_worker_header_denied_for_non_admitted_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Verdict-Worker header + non-admitted model -> 503 fail-closed.

    No upstream call must be made; the response body must name the rejected route.
    """
    transport = _configure(monkeypatch)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "foreign-model",
                "messages": [{"role": "user", "content": "worker task"}],
            },
            headers={"x-verdict-worker": "opencode"},
        )
    assert response.status_code == 503
    # No upstream call must be made — a regression that contacts upstream before
    # rejecting would leave evidence in transport.requests.
    assert transport.requests == [], (
        f"upstream was contacted before rejecting: {transport.requests}"
    )
    # The error message names the rejected route, confirming fail-closed behaviour.
    message = response.json()["error"]["message"]
    assert "foreign-model" in message
    assert "not in the admitted set" in message


_OMIT = object()


@pytest.mark.parametrize(
    "model", [_OMIT, "", None, 123], ids=["omitted", "empty", "null", "non-string"]
)
def test_worker_header_without_valid_model_fails_closed(
    monkeypatch: pytest.MonkeyPatch, model: object
) -> None:
    """X-Verdict-Worker + missing/empty/null/non-string model -> 503, no upstream call.

    A marked worker request must pin a valid admitted model or fail.  It must
    never fall through to the automatically selected route (``other-primary``
    here), which would run a model the worker was not assigned.
    """
    transport = _configure(monkeypatch)
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "worker task"}]}
    if model is not _OMIT:
        body["model"] = model
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions", json=body, headers={"x-verdict-worker": "opencode"}
        )
    assert response.status_code == 503, response.text
    assert transport.requests == [], (
        f"upstream was contacted for a worker request without a valid model: {transport.requests}"
    )
    message = response.json()["error"]["message"]
    assert "worker" in message
    assert "model" in message
