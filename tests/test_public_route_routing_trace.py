"""Opt-in ``routing_trace`` on public ``POST /v1/route`` (BOD-432).

The trace must be the stage outputs of the same request, captured once at the
source, never recomputed in the API and never inferred from the winner.
Real FastAPI app and real IntelligenceService; only gateway reads are faked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.helpers import public_route_harness as h

TRACE_KEYS = {
    "admitted_candidate_ids",
    "shortlist_candidate_ids",
    "optimizer_decision",
    "rejected_offers",
    "admit_drops",
}


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return h.install(monkeypatch, tmp_path)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, svc: Any) -> h.Spy:
    return h.install_spy(monkeypatch, svc)


def _free() -> list[dict[str, Any]]:
    return [h.candidate(route, is_free=True) for route in h.ROUTES]


def _assert_matches_stages(
    trace: dict[str, Any], spy: h.Spy, *, admitted: set[str] | None = None
) -> None:
    """Every trace field equals the real stage output of this one request.

    ``admitted`` defaults to the admission receipt handed to the candidate pool;
    on the live confirmation path the caller passes the confirmed set instead.
    """
    assert len(spy.optimizer) == 1, "optimizer ran more than once for one request"
    assert len(spy.pool_inputs) == 1, "candidate pool built more than once"
    request, decision = spy.optimizer[0]
    expected = set(spy.pool_inputs[0]) if admitted is None else admitted
    assert set(trace["admitted_candidate_ids"]) == expected
    assert trace["shortlist_candidate_ids"] == [
        row.route_id for row in request.pool_receipt.shortlist
    ]
    assert trace["optimizer_decision"]["selected_candidate_id"] == decision.selected_candidate_id
    assert trace["rejected_offers"] == [
        {"candidate_id": item.candidate_id, "code": item.reason.split(";", 1)[0].strip()}
        for item in decision.rejected
        if item.candidate_id is not None
    ]
    # admit_drops: every route offered but not admitted must appear exactly once.
    admitted_ids = set(trace["admitted_candidate_ids"])
    for drop in trace["admit_drops"]:
        assert set(drop) == {"candidate_id", "code"}
        assert drop["candidate_id"] not in admitted_ids, (
            f"route {drop['candidate_id']!r} is both admitted and in admit_drops"
        )


def test_trace_on_selection(spy: h.Spy) -> None:
    """Catches: a trace missing a stage, or one rebuilt from the winner."""
    with h.client() as client:
        response = client.post("/v1/route?include_routing_trace=true", json=h.route_body(_free()))
    assert response.status_code == 200, response.text
    body = response.json()
    trace = body["routing_trace"]
    assert set(trace) == TRACE_KEYS
    assert trace["optimizer_decision"]["outcome"] == "selected"
    assert trace["optimizer_decision"]["selected_candidate_id"] == (
        f"{body['provider']}/{body['model']}"
    )
    # Several admitted candidates: the trace is not just the winner.
    assert len(trace["admitted_candidate_ids"]) == len(h.ROUTES)
    assert trace["rejected_offers"], "losing shortlist offers must appear with their codes"
    _assert_matches_stages(trace, spy)


def test_trace_on_denied(spy: h.Spy) -> None:
    """Catches: no trace when no route qualifies (every offer ineligible)."""
    candidates = [h.candidate(route, eligible=False) for route in h.ROUTES]
    with h.client() as client:
        response = client.post(
            "/v1/route?include_routing_trace=true", json=h.route_body(candidates)
        )
    assert response.status_code == 400, response.text
    trace = response.json()["routing_trace"]
    assert set(trace) == TRACE_KEYS
    assert trace["optimizer_decision"] == {"outcome": "denied", "selected_candidate_id": None}
    assert {item["code"] for item in trace["rejected_offers"]} == {"hard_ineligible_route"}
    _assert_matches_stages(trace, spy)


def test_trace_on_blocked(monkeypatch: pytest.MonkeyPatch, svc: Any, spy: h.Spy) -> None:
    """Catches: a fabricated rejection list when confirmation empties the pool."""
    svc.passports.clear()
    svc.confirm_transport = None

    def failing(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        return {"status_code": 500, "body": {}}

    monkeypatch.setattr(svc, "_resolve_confirm_transport", lambda endpoint: failing)
    with h.client() as client:
        response = client.post("/v1/route?include_routing_trace=true", json=h.route_body(_free()))
    assert response.status_code == 400, response.text
    trace = response.json()["routing_trace"]
    assert set(trace) == TRACE_KEYS
    assert trace["optimizer_decision"] == {"outcome": "denied", "selected_candidate_id": None}
    # Live confirmation path: the admitted set is what confirmation kept (none),
    # not the pre-confirmation receipt; the shortlist still shows what was tried.
    assert trace["admitted_candidate_ids"] == []
    assert set(trace["shortlist_candidate_ids"]) == set(h.ROUTES)
    _assert_matches_stages(trace, spy, admitted=set())


def test_legacy_response_unchanged(spy: h.Spy) -> None:
    """Catches: the trace (or any new key) leaking into the default response."""
    with h.client() as client:
        legacy = client.post("/v1/route", json=h.route_body(_free()))
        explicit_off = client.post(
            "/v1/route?include_routing_trace=false", json=h.route_body(_free())
        )
        traced = client.post("/v1/route?include_routing_trace=true", json=h.route_body(_free()))
    assert legacy.status_code == explicit_off.status_code == traced.status_code == 200
    assert "routing_trace" not in legacy.json()
    assert "routing_trace" not in explicit_off.json()
    # Opting in adds exactly one key; every legacy key is still present.
    assert set(traced.json()) - set(legacy.json()) == {"routing_trace"}
    assert set(legacy.json()) == set(explicit_off.json())


def test_legacy_error_body_unchanged(spy: h.Spy) -> None:
    """Catches: the no-selection 400 body changing shape without opt-in."""
    candidates = [h.candidate(route, eligible=False) for route in h.ROUTES]
    with h.client() as client:
        legacy = client.post("/v1/route", json=h.route_body(candidates))
        traced = client.post("/v1/route?include_routing_trace=true", json=h.route_body(candidates))
    assert legacy.status_code == traced.status_code == 400
    assert set(legacy.json()) == {"detail"}
    assert traced.json()["detail"] == legacy.json()["detail"]


def test_authority_unchanged(spy: h.Spy) -> None:
    """Catches: trace opt-in bypassing the execution-path authority check."""
    body = h.route_body(_free())
    body.pop("execution_path_request")
    with h.client() as client:
        response = client.post("/v1/route?include_routing_trace=true", json=body)
    assert response.status_code == 400
    assert "routing_trace" not in response.json()


def test_trace_rejects_non_boolean_query(spy: h.Spy) -> None:
    """Catches: hand-parsed query params accepting junk as true."""
    with h.client() as client:
        response = client.post("/v1/route?include_routing_trace=maybe", json=h.route_body(_free()))
    assert response.status_code == 422
    assert spy.optimizer == []


def test_trace_carries_ids_and_codes_only(spy: h.Spy) -> None:
    """Catches: evidence payloads, prices or digests leaking through the trace."""
    candidates = [
        h.candidate(route, price=h.priced("1"), credential_pool="secret-pool-name")
        for route in h.ROUTES
    ]
    with h.client() as client:
        response = client.post(
            "/v1/route?include_routing_trace=true", json=h.route_body(candidates)
        )
    assert response.status_code == 200, response.text
    trace = response.json()["routing_trace"]
    allowed = set(h.ROUTES)
    assert set(trace["admitted_candidate_ids"]) <= allowed
    assert set(trace["shortlist_candidate_ids"]) <= allowed
    assert set(trace["optimizer_decision"]) == {"outcome", "selected_candidate_id"}
    for item in trace["rejected_offers"]:
        assert set(item) == {"candidate_id", "code"}
    codes = {item["code"] for item in trace["rejected_offers"]}
    # Priced losers carry "; selected_cash=..." in the optimizer reason; the code does not.
    assert "higher_complete_expected_cost" in codes
    for item in trace["admit_drops"]:
        assert set(item) == {"candidate_id", "code"}
    text = repr(trace)
    for leaked in ("secret-pool-name", "sha256", "usd", "evidence", "cash", "="):
        assert leaked not in text


def test_trace_is_internal_field_not_in_decision_dict() -> None:
    """Catches: ordinary decision serialization exposing the internal trace."""
    from verdict.api import _safe_decision_dict
    from verdict.models import RoutingDecision

    decision = RoutingDecision(
        model="m", provider="p", tier=1, reason="r", routing_trace={"admitted_candidate_ids": []}
    )
    assert "routing_trace" not in _safe_decision_dict(decision)


def test_admit_drops_covers_every_offered_route(spy: h.Spy) -> None:
    """Catches: a dropped route appearing in neither admitted_candidate_ids nor admit_drops.

    HYG routing-trace-missing-admit-drops: the trace must name every offered
    candidate the request dropped, with a code (e.g. no_fresh_passport).
    The invariant: admitted_candidate_ids | {d["candidate_id"] for d in admit_drops}
    == the set of all offered routes, and the two sets are disjoint.
    """
    with h.client() as client:
        response = client.post("/v1/route?include_routing_trace=true", json=h.route_body(_free()))
    assert response.status_code == 200, response.text
    trace = response.json()["routing_trace"]
    offered_set = set(h.ROUTES)
    admitted_set = set(trace["admitted_candidate_ids"])
    drop_set = {item["candidate_id"] for item in trace["admit_drops"]}
    # Partition: every offered route must appear in exactly one of the two sets.
    assert admitted_set | drop_set == offered_set, (
        f"routes missing from trace: {offered_set - admitted_set - drop_set}"
    )
    assert admitted_set & drop_set == set(), (
        f"routes in both admitted and admit_drops: {admitted_set & drop_set}"
    )
    # Every admit_drop entry must carry a non-empty code string.
    for item in trace["admit_drops"]:
        assert item["code"], f"admit_drop for {item['candidate_id']!r} has empty code"
