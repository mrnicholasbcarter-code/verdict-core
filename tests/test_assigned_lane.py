"""The assigned lane: what a verified worker assignment is admitted on (serve-worker-assignment, 5.1).

A hermetic ``IntelligenceService`` (fake admit snapshot, injected passports, an in-memory metadata
snapshot, a temp home and health cache, a temp receipt store) behind the real relay, with a
recording upstream.  Nothing here makes a live call or reads the real home.

The lane narrows to the signed route right after the active and cooling steps, runs every
per-route hard gate on it, skips the text-based second guess (scanner, worthiness, planner
capabilities, Top-K, pack) and mints the route-bound proof.  Each positive case has an unpinned
control that shows the case is not trivially admitted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests import test_live_candidate_shortlist as live
from tests import worker_assignment_helpers as sign
from tests.relay_proof_helpers import (
    ASSIGNMENT_KEY,
    assignment_headers,
    configure_assignment_auth,
    serve_token_headers,
)
from tests.test_proxy_free_tier_admit import _catalog_row, _ok_confirm_transport, _snapshot
from verdict import intelligence
from verdict.eligibility import EligibilityGate
from verdict.free_tier_admit import NamedDrop
from verdict.intelligence import IntelligenceService, prepared_task_profile
from verdict.metadata.records import CapabilityCaps, ModelMetadataRecord, SoftScores
from verdict.metadata.store import MetadataSnapshot
from verdict.models import CONTEXT_WORKER_ASSIGNMENT, ProviderConfig, RoutingDecision
from verdict.proxy import UpstreamProxy
from verdict.receipt_store import ReceiptStore
from verdict.relay import protocol_for_surface
from verdict.worker_assignment import VerifiedWorkerAssignment, verify_assignment

ROUTES = tuple(f"prov{i}/model-{i}" for i in range(1, 7))
ORIGIN = "http://upstream.test/v1"
CHAT = "/v1/chat/completions"


def provider_of(route_id: str) -> str:
    return route_id.split("/", 1)[0]


def metadata_for(
    routes: tuple[str, ...],
    *,
    tools: dict[str, bool | None] | None = None,
    windows: dict[str, int] | None = None,
    missing: tuple[str, ...] = (),
) -> MetadataSnapshot:
    """Core metadata: tools, reasoning, structured and vision known true, streaming unknown.

    No route carries a ``streaming`` fact, as in the real store (0 of 6965 records), so a
    request that requires streaming is dropped ``required_unknown`` unless it is waived.
    """
    tools = tools or {}
    windows = windows or {}
    rows = []
    for index, route in enumerate(routes):
        if route in missing:
            continue
        tools_field = tools.get(route, True)
        rows.append(
            ModelMetadataRecord(
                id=route,
                provider=provider_of(route),
                omniroute_ids=(route,),
                caps=CapabilityCaps(
                    context=live._prov(windows.get(route, 200_000)),
                    tools=None if tools_field is None else live._prov(tools_field),
                    reasoning=live._prov(True),
                    structured=live._prov(True),
                    vision=live._prov(True),
                ),
                scores=SoftScores(aa_coding=live._prov(90.0 - index)),
            )
        )
    return MetadataSnapshot(
        schema_version="1", refreshed_at=live.FETCHED, sources={}, records=tuple(rows)
    )


def build_service(
    now: datetime,
    store: ReceiptStore,
    *,
    routes: tuple[str, ...] = ROUTES,
    extra_catalog: tuple[str, ...] = (),
    passport_for: tuple[str, ...] | None = None,
    metadata: MetadataSnapshot | bool | None = True,
    quota_blocked: tuple[str, ...] = (),
    inactive: tuple[str, ...] = (),
    confirm: Any = "ok",
    top_k: int = 4,
    profile: str = "development",
    gate: EligibilityGate | None = None,
    require_authority: bool | None = None,
    with_snapshot: bool = True,
    latency: dict[str, float] | None = None,
    providers: dict[str, ProviderConfig] | None = None,
) -> IntelligenceService:
    """The hermetic service every lane test routes through."""
    latency = latency or {}
    provider_names = sorted({provider_of(r) for r in (*routes, *extra_catalog) if "/" in r})
    snapshot = _snapshot(
        catalog=[_catalog_row(r) for r in (*routes, *extra_catalog)],
        free_tier=[],
        providers=[
            {"provider": p, "isActive": p not in inactive, "testStatus": "active"}
            for p in provider_names
        ],
    )
    if quota_blocked:
        snapshot = dataclasses.replace(
            snapshot,
            quota_blocks=tuple(
                NamedDrop(p, "quota_exhausted", "account quota exhausted") for p in quota_blocked
            ),
        )
    passports = {
        route: live._passport(route, latency_ms=latency.get(route, 100.0 + index))
        for index, route in enumerate(routes)
        if passport_for is None or route in passport_for
    }
    metadata_snapshot = metadata_for(routes) if metadata is True else (metadata or None)
    return IntelligenceService(
        primary_model="primary/frontier",
        providers=(
            {"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")}
            if providers is None
            else providers
        ),
        profile=profile,
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot if with_snapshot else None,
        execute_offload=False,
        passports=passports,
        confirm_transport=_ok_confirm_transport() if confirm == "ok" else confirm,
        admit_now=now,
        metadata_snapshot=metadata_snapshot,
        metadata_store_path=None
        if metadata_snapshot is not None
        else Path("/nonexistent/none.json"),
        candidate_top_k=top_k,
        eligibility_gate=gate,
        require_execution_path_authority=require_authority,
        receipt_store=store,
        context_roots=(),
        mcp_root="",
    )


def verified(
    route_id: str, *, run_id: str = "run-1", node_id: str = "node-1", ttl: int = 600
) -> VerifiedWorkerAssignment:
    """A verified assignment built the only way production builds one: sign, then verify."""
    now = datetime.now(timezone.utc)
    token = sign.assignment(
        ASSIGNMENT_KEY,
        route_id=route_id,
        run_id=run_id,
        node_id=node_id,
        exp=int(now.timestamp()) + ttl,
        iat=int(now.timestamp()),
    )
    return verify_assignment(token, ASSIGNMENT_KEY, now=now)


def body_for(route: str, text: str = "do the work", **extra: Any) -> dict[str, Any]:
    return {"model": route, "messages": [{"role": "user", "content": text}], **extra}


def lane_context(
    body: dict[str, Any], assignment: VerifiedWorkerAssignment | None
) -> dict[str, Any]:
    """The routing context the relay builds for ``body`` (``api._authority_context`` + origin)."""
    context = api._authority_context(body, task=api._task_text(body))
    context["_dispatch_gateway_origin"] = ORIGIN
    context["_relay_protocol"] = protocol_for_surface("chat")
    if assignment is not None:
        context[CONTEXT_WORKER_ASSIGNMENT] = assignment
    return context


def route_direct(
    service: IntelligenceService,
    body: dict[str, Any],
    assignment: VerifiedWorkerAssignment | None,
    *,
    criticality: str = "medium",
) -> RoutingDecision:
    task = api._task_text(body)
    return asyncio.run(
        service.route(task, criticality=criticality, context=lane_context(body, assignment))
    )


def exclusion_codes(decision: RoutingDecision, route: str) -> list[str]:
    receipt = decision.admit_receipt or {}
    return [row["reason"] for row in receipt.get("exclusions", []) if row["model"] == route]


class Wired:
    """The service behind the real relay, plus everything a test needs to look at."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: IntelligenceService
    ) -> None:
        self.service = service
        self.upstream: list[dict[str, Any]] = []
        self.decisions: list[RoutingDecision] = []
        self.minted: list[dict[str, Any]] = []
        self.contracts: list[dict[str, Any]] = []
        monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health.json"))
        monkeypatch.setenv("LLMGATE_LOG_PATH", "")
        configure_assignment_auth(monkeypatch, tmp_path)

        async def upstream(request: httpx.Request) -> httpx.Response:
            raw = await request.aread()
            body = json.loads(raw) if raw else None
            self.upstream.append(
                {"headers": dict(request.headers), "body": body, "raw": raw.decode()}
            )
            return httpx.Response(
                200,
                json={
                    "model": (body or {}).get("model"),
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

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
        real_contract = api.build_routing_decision_contract

        def recording_contract(*args: Any, **kwargs: Any) -> Any:
            contract = real_contract(*args, **kwargs)
            self.contracts.append(contract.to_dict())
            return contract

        monkeypatch.setattr(api, "build_routing_decision_contract", recording_contract)
        monkeypatch.setattr(api, "_build_intelligence", lambda: service)
        monkeypatch.setattr(
            api,
            "_build_proxy",
            lambda: UpstreamProxy(
                ORIGIN, api_key="gateway-key", transport=httpx.MockTransport(upstream)
            ),
        )

    def post(
        self,
        body: dict[str, Any],
        *,
        assigned: str | None = None,
        run_id: str = "run-1",
        node_id: str = "node-1",
        surface: str = CHAT,
    ) -> httpx.Response:
        headers = (
            serve_token_headers()
            if assigned is None
            else assignment_headers(assigned, run_id=run_id, node_id=node_id)
        )
        with TestClient(api.app) as client:
            return client.post(surface, json=body, headers=headers)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    current = datetime.now(timezone.utc)
    monkeypatch.setattr(live, "NOW", current)  # the passport helper reads this module clock
    return current


@pytest.fixture
def store(tmp_path: Path) -> ReceiptStore:
    return ReceiptStore(tmp_path / "lane-receipts.db")  # unscoped, so a test can read every row


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, now: datetime, store: ReceiptStore
) -> Wired:
    return Wired(monkeypatch, tmp_path, build_service(now, store))


# --- the words that trip the scanner do not refuse the worker ---------------------------------


def test_a_body_with_payment_is_admitted_and_served_while_the_unpinned_control_is_refused(
    wired: Wired,
) -> None:
    text = "update the payment webhook handler and the billing refund path"
    control = wired.post(body_for(ROUTES[0], text))
    assert control.status_code == 503, control.text
    assert "never offload" in wired.decisions[-1].reason  # the scanner's money-path floor
    assert wired.upstream == []
    wired.decisions.clear()

    response = wired.post(body_for(ROUTES[0], text), assigned=ROUTES[0])
    assert response.status_code == 200, response.text
    assert [row["body"]["model"] for row in wired.upstream] == [ROUTES[0]]
    decision = wired.decisions[-1]
    assert decision.decision == "selected"
    assert decision.escalated is False and decision.escalation_reason is None
    assert "worker_assigned_lane" in decision.safety_flags


def test_the_unpinned_control_with_credential_words_is_floored_and_the_lane_is_not(
    wired: Wired,
) -> None:
    text = "rotate the credential and the session token in the secret store"
    pinned = wired.post(body_for(ROUTES[1], text), assigned=ROUTES[1])
    assert pinned.status_code == 200, pinned.text
    assert wired.decisions[-1].escalated is False


# --- tools + stream: admitted, and the streaming waiver is recorded -------------------------


TOOLS = [{"type": "function", "function": {"name": "run", "parameters": {"type": "object"}}}]


def test_tools_and_stream_are_admitted_and_the_waiver_is_recorded_everywhere(
    now: datetime, store: ReceiptStore
) -> None:
    service = build_service(now, store)
    assignment = verified(ROUTES[2], run_id="run-9", node_id="node-4")
    body = body_for(ROUTES[2], tools=TOOLS, stream=True)

    # The unpinned control still requires streaming: no route has the fact, so it is dropped.
    control = route_direct(service, body, None)
    assert control.decision == "denied"
    assert any(code == "required_unknown" for r in ROUTES for code in exclusion_codes(control, r))

    decision = route_direct(service, body, assignment)
    assert decision.decision == "selected", decision.reason
    assert decision.model == ROUTES[2]
    waiver = {
        "requirement": "streaming",
        "reason": "verified_worker_assignment",
        "run_id": "run-9",
        "node_id": "node-4",
    }
    record = {
        "route_id": ROUTES[2],
        "run_id": "run-9",
        "node_id": "node-4",
        "expires_at": assignment.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assignment_digest": assignment.assignment_digest,
    }
    receipt = decision.admit_receipt or {}
    assert receipt["requirement_waivers"] == [waiver]
    assert receipt["worker_assignment"] == record
    assert "streaming" not in receipt["requirements"] and "tools" in receipt["requirements"]
    for flag in (
        "worker_assigned_lane",
        "worker_assignment_streaming_waived",
        "free_tier_active_admit",
        "prove_confirm_admit",
        "capability_hard_gate",
    ):
        assert flag in decision.safety_flags
    assert not any(flag.startswith("worthiness_") for flag in decision.safety_flags)
    assert "cheap_path_context_pack" not in decision.safety_flags
    assert decision.tier == 3 and decision.protected is False

    # The evidence record carries them (the compact admit receipt is an allowlist).
    from verdict.evidence import build_routing_decision_contract

    contract = build_routing_decision_contract(
        decision, task=api._task_text(body), criticality="medium"
    ).to_dict()
    assert contract["receipt"]["requirement_waivers"] == [waiver]
    assert contract["receipt"]["worker_assignment"] == record
    assert contract["policy_floor"] == "best_effort"  # the lane claims no floor it never computed

    # The persisted routing receipt carries them in its extensions.
    lane_id = receipt["receipt_id"]
    roots = [
        r
        for r in store.query_receipts(receipt_type="decision")
        if not r.parent_receipt_id and r.payload["attempt_id"] == lane_id
    ]
    assert len(roots) == 1  # the control's denied receipt is a different attempt
    extensions = roots[0].payload["extensions"]
    assert extensions["requirement_waivers"] == [waiver]
    assert extensions["worker_assignment"] == record


def test_streaming_is_waived_for_streaming_required_too_and_only_for_an_assignment(
    now: datetime, store: ReceiptStore
) -> None:
    service = build_service(now, store)
    body = body_for(ROUTES[0], streaming_required=True)
    assert route_direct(service, body, verified(ROUTES[0])).decision == "selected"
    assert route_direct(service, body, None).decision == "denied"


def test_tools_stay_required_on_the_lane(now: datetime, store: ReceiptStore) -> None:
    service = build_service(now, store, metadata=metadata_for(ROUTES, tools={ROUTES[0]: False}))
    body = body_for(ROUTES[0], tools=TOOLS)
    decision = route_direct(service, body, verified(ROUTES[0]))
    assert decision.decision == "denied"
    assert "capability_mismatch" in exclusion_codes(decision, ROUTES[0])
    assert decision.admission_proofs == {}


# --- size: no pack, no cap ---------------------------------------------------------------


def _plain_text(characters: int) -> str:
    unit = "alpha beta gamma delta epsilon "
    return (unit * (characters // len(unit) + 1))[:characters]


def test_a_130528_character_request_is_admitted_where_the_unpinned_ladder_denies_at_34000(
    now: datetime, store: ReceiptStore
) -> None:
    service = build_service(now, store)
    control = route_direct(service, body_for(ROUTES[0], _plain_text(34_000)), None)
    assert control.decision == "denied"
    assert "task_instructions_omitted" in control.safety_flags

    big = body_for(ROUTES[0], _plain_text(130_528))
    decision = route_direct(service, big, verified(ROUTES[0]))
    assert decision.decision == "selected", decision.reason
    assert "cheap_path_context_pack" not in decision.safety_flags
    assert decision.context_pack_prompt is None


def test_the_pinned_route_the_top_k_would_starve_is_admitted(
    now: datetime, store: ReceiptStore
) -> None:
    # Latency orders the shortlist, so the slowest route is the one Top-4 leaves out.
    latency = {route: 100.0 * (index + 1) for index, route in enumerate(ROUTES)}
    service = build_service(now, store, top_k=4, latency=latency)
    body = body_for(ROUTES[-1])
    control = route_direct(service, body, None)
    starved = [
        route
        for route in ROUTES
        if "candidate_pool_not_shortlisted" in exclusion_codes(control, route)
    ]
    assert starved, "the unpinned ladder must leave at least one route out of the Top-4"
    for route in starved:
        decision = route_direct(service, body_for(route), verified(route))
        assert decision.decision == "selected", (route, decision.reason)
        assert decision.model == route


# --- the body is forwarded verbatim; the proof verifies at the relay --------------------------


def test_the_upstream_body_is_the_request_except_model_with_nothing_added(wired: Wired) -> None:
    body = {
        "model": ROUTES[3],
        "messages": [
            {"role": "system", "content": "you are a worker"},
            {"role": "user", "content": "do the work now"},
        ],
        "temperature": 0.2,
        "tools": TOOLS,
        "max_tokens": 64,
    }
    response = wired.post(body, assigned=ROUTES[3])
    assert response.status_code == 200, response.text
    assert len(wired.upstream) == 1
    sent = wired.upstream[0]["body"]
    assert sent == {**body, "model": ROUTES[3]}
    assert len(sent["messages"]) == 2  # no pack message was injected
    assert "cheap_path_context_pack" not in wired.decisions[-1].safety_flags


@pytest.mark.parametrize("surface", [CHAT, "/v1/responses"])
def test_the_lane_proof_verifies_at_the_relay_for_text_that_makes_the_planner_add_tools(
    wired: Wired, surface: str
) -> None:
    text = "implement the api, build it and add a test for the parser"
    body = body_for(ROUTES[4], text)
    if surface == "/v1/responses":
        body = {"model": ROUTES[4], "input": text}
    response = wired.post(body, assigned=ROUTES[4], surface=surface)
    assert response.status_code == 200, response.text
    assert [row["body"]["model"] for row in wired.upstream] == [ROUTES[4]]
    decision = wired.decisions[-1]
    proof = decision.admission_proofs[ROUTES[4]]
    task = api._task_text(body)
    context = lane_context(body, verified(ROUTES[4]))
    relay_digest = prepared_task_profile(task, context, assigned=True)[1].digest
    assert proof.scope.profile_digest == relay_digest
    # The planner would have added tool-calling for this text; the lane does not.
    assert (decision.admit_receipt or {})["requirements"] == []


def test_a_proof_with_streaming_waived_still_matches_the_relay_digest(wired: Wired) -> None:
    body = body_for(ROUTES[0], tools=TOOLS, stream=True)
    decision = route_direct(wired.service, body, verified(ROUTES[0]))
    assert decision.decision == "selected"
    expected = prepared_task_profile(
        api._task_text(body), lane_context(body, verified(ROUTES[0])), assigned=True
    )[1].digest
    assert decision.admission_proofs[ROUTES[0]].scope.profile_digest == expected


# --- receipt ids: an injective encoding of the signed ids ---------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(("a|b", "c"), ("a", "b|c"), id="pipe-delimiter"),
        pytest.param(('a","b', "c"), ("a", 'b","c'), id="quote-comma-delimiter"),
    ],
)
def test_ids_that_hold_the_delimiter_characters_keep_distinct_receipt_ids(
    now: datetime, store: ReceiptStore, first: tuple[str, str], second: tuple[str, str]
) -> None:
    service = build_service(now, store)
    body = body_for(ROUTES[0])
    one = route_direct(service, body, verified(ROUTES[0], run_id=first[0], node_id=first[1]))
    two = route_direct(service, body, verified(ROUTES[0], run_id=second[0], node_id=second[1]))
    again = route_direct(service, body, verified(ROUTES[0], run_id=first[0], node_id=first[1]))
    ids = [d.admit_receipt["receipt_id"] for d in (one, two, again)]  # type: ignore[index]
    assert ids[0] != ids[1]  # different assignments never share a receipt id
    assert ids[0] == ids[2]  # the same assignment sent twice gets the same id
    assert all(value.startswith("vrct_") and len(value) == 5 + 24 for value in ids)
    persisted = {
        r.payload["attempt_id"]
        for r in store.query_receipts(receipt_type="decision")
        if not r.parent_receipt_id
    }
    assert {ids[0], ids[1]} <= persisted  # the persisted routing attempt ids differ with them
    assert len(persisted) == 2


def test_the_receipt_id_is_set_before_the_routing_receipt_is_persisted(
    now: datetime, store: ReceiptStore
) -> None:
    """Without it the attempt id falls back to a hash of the route alone, shared by every node."""
    service = build_service(now, store)
    one = route_direct(service, body_for(ROUTES[0]), verified(ROUTES[0], node_id="n1"))
    two = route_direct(service, body_for(ROUTES[0]), verified(ROUTES[0], node_id="n2"))
    attempts = {
        r.payload["attempt_id"]
        for r in store.query_receipts(receipt_type="decision")
        if not r.parent_receipt_id
    }
    first, second = one.admit_receipt["receipt_id"], two.admit_receipt["receipt_id"]  # type: ignore[index]
    assert first != second  # two nodes on one route do not share an id
    assert attempts == {first, second}
    assert not any(a.startswith("admit-") for a in attempts)


# --- secrets ------------------------------------------------------------------------------------


def test_the_token_the_tag_and_the_key_appear_nowhere_serve_writes(
    wired: Wired, store: ReceiptStore, caplog: pytest.LogCaptureFixture
) -> None:
    headers = assignment_headers(ROUTES[0], run_id="run-s", node_id="node-s")
    token = headers["authorization"].split(" ", 1)[1]
    tag = sign.tag_part(token)
    key_text = sign.b64url(ASSIGNMENT_KEY)
    with caplog.at_level(logging.DEBUG), TestClient(api.app) as client:
        response = client.post(CHAT, json=body_for(ROUTES[0]), headers=headers)
    assert response.status_code == 200, response.text
    written = [
        response.text,
        json.dumps(dict(response.headers)),
        json.dumps(wired.contracts),
        json.dumps([d.admit_receipt for d in wired.decisions], default=str),
        json.dumps([r.payload for r in store.query_receipts(limit=1000)], default=str),
        json.dumps(wired.upstream),
        caplog.text,
    ]
    for secret in (token, tag, key_text, sign.payload_part(token)):
        for text in written:
            assert secret not in text
    # The upstream request carries the gateway's credential only.
    assert wired.upstream[0]["headers"]["authorization"] == "Bearer gateway-key"
    # The assignment is named by digest and ids, which are in the evidence.
    assert "run-s" in json.dumps(wired.contracts) and "node-s" in json.dumps(wired.contracts)
