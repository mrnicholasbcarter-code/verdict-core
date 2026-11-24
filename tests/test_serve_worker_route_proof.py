"""Worker route pinning with production-shaped admission proofs (ADR-053 D1, ADR-035).

``tests/test_serve_route_pinning.py`` covers the relay's request validation with a
double that pre-mints proofs.  Here the real ``IntelligenceService`` builds the
decision, so the proof map is what production produces: **chosen route only**.  A
worker pinned to an admitted route other than serve's own pick must still dispatch,
with that route's own proof, and only when the route is in ``receipt.admitted``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
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
WORKER = {"x-verdict-worker": "opencode"}


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
        execute_offload=False,
        passports=passports,
        confirm_transport=_ok_confirm_transport(),
        admit_now=now,
        context_roots=(),
        mcp_root="",
    )


class _Harness:
    """Real service + recording upstream, with the real proof minter observed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, service: IntelligenceService) -> None:
        self.transport = RecordingTransport()
        self.decisions: list[RoutingDecision] = []
        self.minted: list[dict[str, Any]] = []
        _configure(monkeypatch, self.transport, service)

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

    def post(self, client: TestClient, *, worker: bool, **body: Any) -> Any:
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": "summarize this paragraph"}],
            "max_tokens": 16,
            "criticality": "low",
            **body,
        }
        return client.post("/v1/chat/completions", json=payload, headers=WORKER if worker else {})

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chosen-only proofs: the pinned alternative is dispatched on its own minted proof."""
    harness = _Harness(monkeypatch, _service())
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
    # Admission is unchanged: the pin selects within the admitted set, never widens it.
    assert set(decision.admit_receipt["admitted"]) == unpinned_admitted  # type: ignore[index]
    assert decision.admit_receipt["chosen"] == pinned  # type: ignore[index]


@pytest.mark.parametrize(
    "assigned",
    ["openrouter/not/in-the-catalog:free", "auto/best", "combo/anything"],
    ids=["unknown-route", "opaque-auto", "opaque-combo"],
)
def test_worker_pin_to_route_outside_the_admitted_set_is_denied_without_a_proof(
    monkeypatch: pytest.MonkeyPatch, assigned: str
) -> None:
    harness = _Harness(monkeypatch, _service())
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=assigned)

    assert response.status_code == 503, response.text
    assert harness.transport.requests == [], "upstream was contacted for a non-admitted route"
    assert harness.minted == [], "a proof was minted for a route outside the admitted set"
    decision = harness.decisions[-1]
    assert decision.decision == "denied"
    assert decision.admission_proofs == {}
    message = response.json()["error"]["message"]
    assert assigned in message and "not in the admitted set" in message


def test_worker_pin_requires_membership_in_this_requests_admitted_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the catalog is not enough: with no fresh passport the route is not admitted."""
    harness = _Harness(monkeypatch, _service(passport_for=(FREE,)))
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=SUBSCRIPTION)

    assert response.status_code == 503, response.text
    assert harness.transport.requests == []
    assert harness.minted == []
    assert SUBSCRIPTION not in harness.decisions[-1].admit_receipt["admitted"]  # type: ignore[index]


def test_non_worker_request_with_a_model_field_behaves_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the worker header the ``model`` field is ignored for routing, as before.

    A client also cannot supply the internal pin: underscore keys are dropped from
    the public payload, and only the relay sets the typed value.
    """
    harness = _Harness(monkeypatch, _service())
    with TestClient(api.app) as client:
        default = _default_choice(harness, client)
        other = SUBSCRIPTION if default == FREE else FREE
        harness.reset()

        response = harness.post(client, worker=False, model=other, _worker_assigned_route=other)

    assert response.status_code == 200, response.text
    assert response.headers["x-verdict-model"] == default
    assert [r["body"]["model"] for r in harness.transport.requests] == [default]
    assert [list(proofs) for proofs in harness.minted] == [[default]]
    assert harness.decisions[-1].model == default
