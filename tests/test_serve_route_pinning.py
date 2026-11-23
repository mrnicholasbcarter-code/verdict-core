"""Route-pinning tests for the OpenCode worker adapter (ADR-053 Decision 1, Task 2.4).

These tests cover the per-request route pinning path added for OpenCode workers:
when ``X-Verdict-Worker`` is present, the ``model`` field in the request body is
treated as the route the worker was assigned, and Verdict either narrows the
decision to that exact route (if admitted) or fails closed with 503 (if not).

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
    """Intelligence that returns a decision with one admitted route in
    candidate_states, without asserting the task text."""

    async def route(
        self,
        task: str,
        criticality: str = "medium",
        context: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> RoutingDecision:
        decision = RoutingDecision(
            model="selected-model",
            provider="omniroute",
            tier=2,
            reason="worker pinning test fixture",
            request_id="pin-req-1",
            managed_backend_status="healthy",
            quality_outcome="unknown",
            # Only "selected-model" is admitted; any other model in the request
            # body must be denied fail-closed.
            candidate_states=[{"model_id": "selected-model", "admitted": True}],
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


def test_worker_header_pins_admitted_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Verdict-Worker header + admitted model in body -> decision uses that model.

    The upstream request must carry the pinned route and the response must be 200.
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
    assert response.status_code == 200
    assert len(transport.requests) == 1
    assert transport.requests[0]["body"]["model"] == "selected-model"


def test_worker_header_denied_for_non_admitted_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Verdict-Worker header + non-admitted model -> 503 fail-closed.

    No upstream call must be made; the response body must name the rejected route.
    """
    _configure(monkeypatch)
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
    # The error message names the rejected route, confirming fail-closed behaviour.
    message = response.json()["error"]["message"]
    assert "foreign-model" in message
    assert "not in the admitted set" in message
