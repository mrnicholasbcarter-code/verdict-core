"""Worker route pinning with production-shaped admission proofs (ADR-053 D1, ADR-035).

``tests/test_serve_route_pinning.py`` covers the relay's request validation with a
double that pre-mints proofs.  Here the real ``IntelligenceService`` builds the
decision, so the proof map is what production produces: **chosen route only**.  A
worker assigned an admitted route other than serve's own pick must still dispatch,
with that route's own proof, and only when the route is in ``receipt.admitted``.

The route comes only from a verified signed assignment (the worker's bearer); the
``X-Verdict-Worker`` header alone selects nothing.  No test reads the real home: the
signing key lives in a temp home.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
import verdict.orchestration.executor_opencode as executor_module
from tests.relay_proof_helpers import (
    ASSIGNMENT_KEY,
    assignment_headers,
    assignment_token,
    configure_assignment_auth,
    serve_token_headers,
)
from tests.test_assigned_lane import metadata_for
from tests.test_proxy_free_tier_admit import (
    RecordingTransport,
    _catalog_row,
    _configure,
    _ok_confirm_transport,
    _snapshot,
)
from verdict import intelligence
from verdict.intelligence import IntelligenceService
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig, RoutingDecision

FREE = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
SUBSCRIPTION = "subscription/strong-code"
PROVIDER = {FREE: "openrouter", SUBSCRIPTION: "subscription"}


def _service(*, passport_for: tuple[str, ...] = (FREE, SUBSCRIPTION)) -> IntelligenceService:
    now = datetime.now(timezone.utc)
    snapshot = _snapshot(
        catalog=[_catalog_row(FREE), _catalog_row(SUBSCRIPTION)],
        free_tier=[
            {"modelId": FREE.split("/", 1)[1], "provider": "openrouter", "freeType": "keyless"}
        ],
        providers=[
            {"provider": "openrouter", "isActive": True, "testStatus": "active"},
            {"provider": "subscription", "isActive": True, "testStatus": "active"},
        ],
    )
    passports = {
        identity: ModelPassport(
            provider=PROVIDER[identity],
            model_id=identity,
            auth_state="authorized",
            availability_state="eligible",
            qualified_at=now - timedelta(minutes=1),
            last_verified_timestamp=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=10),
        )
        for identity in passport_for
    }
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus-20240229",
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        # The assigned lane reads real metadata and denies without it (fail closed).
        metadata_snapshot=metadata_for((FREE, SUBSCRIPTION)),
        execute_offload=False,
        passports=passports,
        confirm_transport=_ok_confirm_transport(),
        admit_now=now,
        context_roots=(),
        mcp_root="",
    )


class _Harness:
    """Real service + recording upstream, with the real proof minter observed."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, service: IntelligenceService, home: Path
    ) -> None:
        self.transport = RecordingTransport()
        self.decisions: list[RoutingDecision] = []
        self.minted: list[dict[str, Any]] = []
        _configure(monkeypatch, self.transport, service)
        configure_assignment_auth(monkeypatch, home)  # token mode, key in the temp home

        real_route = service.route

        async def spy_route(*args: Any, **kwargs: Any) -> RoutingDecision:
            decision = await real_route(*args, **kwargs)
            self.decisions.append(decision)
            return decision

        monkeypatch.setattr(service, "route", spy_route)
        real_proof = intelligence._chosen_route_proof

        def recording(*args: Any, **kwargs: Any) -> dict[str, Any]:
            proofs = real_proof(*args, **kwargs)
            self.minted.append(proofs)
            return proofs

        monkeypatch.setattr(intelligence, "_chosen_route_proof", recording)

    def post(
        self,
        client: TestClient,
        *,
        worker: bool,
        assigned: str | None = None,
        headers: dict[str, str] | None = None,
        **body: Any,
    ) -> Any:
        """``worker``: the bearer is a signed assignment for ``assigned`` (default: ``model``)."""
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": "summarize this paragraph"}],
            "max_tokens": 16,
            "criticality": "low",
            **body,
        }
        if headers is None:
            headers = (
                assignment_headers(assigned or str(body["model"]))
                if worker
                else serve_token_headers()
            )
        return client.post("/v1/chat/completions", json=payload, headers=headers)

    def reset(self) -> None:
        self.transport.requests.clear()
        self.decisions.clear()
        self.minted.clear()


def _default_choice(harness: _Harness, client: TestClient) -> str:
    """The route serve picks for an unpinned request (what the worker must override)."""
    response = harness.post(client, worker=False, model="auto")
    assert response.status_code == 200, response.text
    chosen = response.headers["x-verdict-model"]
    assert chosen in PROVIDER
    return chosen


def test_worker_pins_admitted_alternative_with_its_own_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chosen-only proofs: the pinned alternative is dispatched on its own minted proof."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    with TestClient(api.app) as client:
        default = _default_choice(harness, client)
        unpinned_admitted = set(harness.decisions[-1].admit_receipt["admitted"])  # type: ignore[index]
        pinned = SUBSCRIPTION if default == FREE else FREE
        assert pinned in unpinned_admitted, "fixture must admit both routes"
        harness.reset()

        response = harness.post(client, worker=True, model=pinned)

    assert response.status_code == 200, response.text
    assert response.headers["x-verdict-model"] == pinned
    assert [r["body"]["model"] for r in harness.transport.requests] == [pinned]
    # Exactly one proof was minted, for the pinned route, by the shared chosen-route minter.
    assert [list(proofs) for proofs in harness.minted] == [[pinned]]
    proof = harness.minted[0][pinned]
    assert (proof.route_id, proof.model, proof.provider) == (pinned, pinned, PROVIDER[pinned])

    decision = harness.decisions[-1]
    assert decision.model == pinned
    assert decision.provider == PROVIDER[pinned]  # provider follows the pinned route
    assert list(decision.admission_proofs) == [pinned]
    assert decision.alternatives == [], "a pinned request must not fall back to other routes"
    # No stale route identity from the route serve would have chosen.
    assert [f for f in decision.safety_flags if f.startswith("bod104_route_id:")] in (
        [],
        [f"bod104_route_id:{pinned}"],
    )
    # The lane narrows to the pinned route right after the active and cooling steps: its admitted
    # set is that one route, and the route was in the unpinned ladder's set (nothing re-admitted).
    assert decision.admit_receipt["admitted"] == [pinned]  # type: ignore[index]
    assert pinned in unpinned_admitted
    assert decision.admit_receipt["chosen"] == pinned  # type: ignore[index]


@pytest.mark.parametrize(
    ("assigned", "reason"),
    [
        ("openrouter/not/in-the-catalog:free", "route_not_in_inventory"),
        ("auto/best", "opaque_route_disallowed"),
        ("combo/anything", "opaque_route_disallowed"),
    ],
    ids=["unknown-route", "opaque-auto", "opaque-combo"],
)
def test_worker_pin_to_route_outside_the_admitted_set_is_denied_without_a_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, assigned: str, reason: str
) -> None:
    harness = _Harness(monkeypatch, _service(), tmp_path)
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=assigned)

    assert response.status_code == 503, response.text
    assert harness.transport.requests == [], "upstream was contacted for a non-admitted route"
    assert harness.minted == [], "a proof was minted for a route outside the admitted set"
    decision = harness.decisions[-1]
    assert decision.decision == "denied"
    assert decision.admission_proofs == {}
    # The reason is the one the check that dropped the route gave, and no other route was chosen.
    message = response.json()["error"]["message"]
    assert assigned in message and reason in message
    assert decision.model == "no_eligible_target" and decision.alternatives == []


def test_worker_pin_requires_membership_in_this_requests_admitted_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In the catalog is not enough: with no fresh passport the route is not admitted."""
    harness = _Harness(monkeypatch, _service(passport_for=(FREE,)), tmp_path)
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=SUBSCRIPTION)

    assert response.status_code == 503, response.text
    assert harness.transport.requests == []
    assert harness.minted == []
    assert SUBSCRIPTION not in harness.decisions[-1].admit_receipt["admitted"]  # type: ignore[index]


def test_non_worker_request_with_a_model_field_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an assignment the ``model`` field is ignored for routing, as before.

    A client also cannot supply the internal context key: underscore keys are dropped from
    the public payload, and only the relay sets the typed value from a verified assignment.
    """
    harness = _Harness(monkeypatch, _service(), tmp_path)
    with TestClient(api.app) as client:
        default = _default_choice(harness, client)
        other = SUBSCRIPTION if default == FREE else FREE
        harness.reset()

        response = harness.post(
            client,
            worker=False,
            model=other,
            _worker_assignment={"route_id": other},
            _worker_assigned_route=other,
        )

    assert response.status_code == 200, response.text
    assert response.headers["x-verdict-model"] == default
    assert [r["body"]["model"] for r in harness.transport.requests] == [default]
    assert [list(proofs) for proofs in harness.minted] == [[default]]
    assert harness.decisions[-1].model == default


def test_client_model_override_flag_does_not_change_relay_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On main the relay never honours ``allow_client_model_override`` (the flag is only
    stored); admission mints a proof for its own pick only, so rewriting the model after
    admission could only fail closed.  Non-worker routing is unchanged, flag or not."""
    service = _service()
    service.allow_client_model_override = True
    harness = _Harness(monkeypatch, service, tmp_path)
    with TestClient(api.app) as client:
        default = _default_choice(harness, client)
        other = SUBSCRIPTION if default == FREE else FREE
        harness.reset()
        response = harness.post(client, worker=False, model=other)

    assert response.status_code == 200, response.text
    assert response.headers["x-verdict-model"] == default
    assert [r["body"]["model"] for r in harness.transport.requests] == [default]


def test_the_serve_token_with_the_worker_header_pins_nothing_on_the_real_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #1299 hole: serve token + header + a model naming an admitted route.  Now refused."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    with TestClient(api.app) as client:
        default = _default_choice(harness, client)
        other = SUBSCRIPTION if default == FREE else FREE
        harness.reset()
        response = harness.post(
            client,
            worker=False,
            model=other,
            headers={**serve_token_headers(), "x-verdict-worker": "opencode"},
        )

    assert response.status_code == 403, response.text
    assert harness.decisions == [], "the request reached routing"
    assert harness.minted == [], "a proof was minted for a header-only request"
    assert harness.transport.requests == []


def test_a_verified_assignment_for_another_route_than_the_model_is_refused_before_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _Harness(monkeypatch, _service(), tmp_path)
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, assigned=FREE, model=SUBSCRIPTION)

    assert response.status_code == 400, response.text
    assert harness.decisions == []
    assert harness.minted == []
    assert harness.transport.requests == []


def test_the_orchestrators_assignment_probe_is_refused_before_routing_and_costs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The orchestrator RELIES ON this order (#1340).

    Before it launches an OpenCode worker, ``OpenCodeExecutor`` proves that this serve accepts the
    attempt's own signed assignment by sending the assignment as the bearer on
    ``POST /v1/chat/completions`` with a body that is only ``{"model": <a value no route can
    equal>}``: no ``messages``.  Serve verifies the assignment in its middleware, and then the
    relay compares ``model`` with the assignment's route and refuses a difference with 400 before
    the ladder runs.  So a 400 tells the orchestrator "verified", while a refused assignment never
    gets that far (403, or 503 without a key).  If serve ever routed a request whose model does not
    equal its assignment's route, or checked the body after routing, the probe would become a
    routing decision, an admission proof and an upstream call (a spend): this test would fail
    first, and the executor's probe would have to change with it.

    Serve logs one WARNING (``reason=model_mismatch``) for every probe.  That is accepted and
    documented, not silenced.
    """
    harness = _Harness(monkeypatch, _service(), tmp_path)
    probe_body = executor_module._assignment_probe_body()
    assert json.loads(probe_body) == {"model": executor_module._ASSIGNMENT_PROBE_MODEL}
    headers = {**assignment_headers(FREE), "content-type": "application/json"}
    with caplog.at_level(logging.WARNING, logger="verdict.api"), TestClient(api.app) as client:
        response = client.post("/v1/chat/completions", content=probe_body, headers=headers)

    assert response.status_code == 400, response.text
    assert response.json() == {
        "error": {
            "message": "the request's model must equal the route in the worker assignment",
            "type": "invalid_request_error",
        }
    }
    assert harness.decisions == []
    assert harness.minted == []
    assert harness.transport.requests == []
    refusals = [r.getMessage() for r in caplog.records if "model_mismatch" in r.getMessage()]
    assert len(refusals) == 1 and "reason=model_mismatch" in refusals[0]
    assert headers["authorization"].split(" ", 1)[1] not in caplog.text  # never the credential


def test_route_refuses_a_worker_assignment_combined_with_an_execution_path_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defence in depth behind the relay's 400 (the accepted contract, operator decision
    2026-11-24): ``route()`` itself refuses the combination, so no caller can get a worker
    request selected through the execution-path flow, which never pins."""
    import asyncio

    from verdict.execution_path import ExecutionPathError
    from verdict.models import CONTEXT_WORKER_ASSIGNMENT
    from verdict.worker_assignment import verify_assignment

    harness = _Harness(monkeypatch, _service(), tmp_path)
    verified = verify_assignment(
        assignment_token(SUBSCRIPTION), ASSIGNMENT_KEY, now=datetime.now(timezone.utc)
    )
    context = {
        "execution_path_request": {"schema_version": "anything"},
        CONTEXT_WORKER_ASSIGNMENT: verified,
    }
    service = api._build_intelligence()
    with pytest.raises(ExecutionPathError, match="worker"):
        asyncio.run(service.route("summarize this", context=context))
    assert harness.minted == []
    assert harness.transport.requests == []
