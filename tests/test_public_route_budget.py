"""Public ``POST /v1/route`` hard spend budget (BOD-432, spec route-selection).

Runs the real FastAPI app and the real IntelligenceService; only gateway reads
are faked (tests/helpers/public_route_harness.py) and sockets are blocked.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import public_route_harness as h

FIELD = "execution_path_request.hard_spend_budget"
CHEAP, MIDDLE, LAST = "kr/claude-haiku-5", "kr/claude-sonnet-5", "kr/claude-opus-5"


def _free_candidates() -> list[dict[str, Any]]:
    return [h.candidate(route, is_free=True) for route in h.ROUTES]


def _selected(body: dict[str, Any]) -> str:
    return f"{body['provider']}/{body['model']}"


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return h.install(monkeypatch, tmp_path)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, svc: Any) -> h.Spy:
    return h.install_spy(monkeypatch, svc)


def _sent_budget(spy: h.Spy) -> float | None:
    assert len(spy.optimizer) == 1
    request, _ = spy.optimizer[0]
    return request.hard_spend_budget


# --- accepted input -------------------------------------------------------


def test_valid_integer_budget(spy: h.Spy) -> None:
    """Catches: an int budget dropped at the API boundary instead of enforced."""
    with h.client() as client:
        response = client.post(
            "/v1/route", json=h.route_body(_free_candidates(), budget=5, omit_budget=False)
        )
    assert response.status_code == 200, response.text
    assert _sent_budget(spy) == 5


def test_valid_fractional_budget(spy: h.Spy) -> None:
    """Catches: a float budget truncated or ignored instead of enforced."""
    with h.client() as client:
        response = client.post(
            "/v1/route", json=h.route_body(_free_candidates(), budget=0.01, omit_budget=False)
        )
    assert response.status_code == 200, response.text
    assert _sent_budget(spy) == 0.01


def test_omitted_budget(spy: h.Spy) -> None:
    """Catches: an absent key turned into a default budget."""
    with h.client() as client:
        response = client.post("/v1/route", json=h.route_body(_free_candidates()))
    assert response.status_code == 200, response.text
    assert _sent_budget(spy) is None


# --- rejected input: 400 naming the field, before any probe/authority work -


def _assert_rejected_early(spy: h.Spy, literal: str) -> None:
    with h.client() as client:
        before = spy.snapshot_loads
        response = h.post_raw(client, h.route_body(_free_candidates()), literal)
        traced = h.post_raw(
            client, h.route_body(_free_candidates()), literal, "?include_routing_trace=true"
        )
    for item in (response, traced):
        assert item.status_code == 400, (literal, item.text)
        assert FIELD in item.json()["detail"], (literal, item.text)
        # An input error is not a routing decision: no trace even when asked.
        assert "routing_trace" not in item.json()
    assert spy.snapshot_loads == before, "admit snapshot read on invalid input"
    assert spy.pool_inputs == [], "candidate pool built on invalid input"
    assert spy.optimizer == [], "optimizer ran on invalid input"


def test_explicit_null_rejected(spy: h.Spy) -> None:
    """Catches: explicit null treated like an omitted key."""
    _assert_rejected_early(spy, "null")


@pytest.mark.parametrize("literal", ["true", "false"])
def test_boolean_rejected(spy: h.Spy, literal: str) -> None:
    """Catches: bool accepted because bool is an int subclass in Python."""
    _assert_rejected_early(spy, literal)


def test_string_rejected(spy: h.Spy) -> None:
    """Catches: numeric strings coerced to a budget."""
    _assert_rejected_early(spy, '"5"')


def test_array_rejected(spy: h.Spy) -> None:
    _assert_rejected_early(spy, "[]")


def test_object_rejected(spy: h.Spy) -> None:
    _assert_rejected_early(spy, "{}")


@pytest.mark.parametrize("literal", ["0", "0.0"])
def test_zero_rejected(spy: h.Spy, literal: str) -> None:
    """Catches: a zero budget silently meaning 'free only'."""
    _assert_rejected_early(spy, literal)


@pytest.mark.parametrize("literal", ["-1", "-0.5"])
def test_negative_rejected(spy: h.Spy, literal: str) -> None:
    _assert_rejected_early(spy, literal)


def test_nan_rejected(spy: h.Spy) -> None:
    """Catches: NaN passing because every comparison with NaN is False."""
    _assert_rejected_early(spy, "NaN")


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity"])
def test_infinity_rejected(spy: h.Spy, literal: str) -> None:
    """Catches: an infinite budget disabling the cap."""
    _assert_rejected_early(spy, literal)


@pytest.mark.parametrize("literal", ["1e400", "1" + "0" * 400])
def test_overflow_rejected(spy: h.Spy, literal: str) -> None:
    """Catches: 1e400 parsing to inf, and a huge integer raising OverflowError (500)."""
    _assert_rejected_early(spy, literal)


# --- enforcement through the real qualifier --------------------------------


def _cash(spy: h.Spy, route: str) -> Decimal:
    request, _ = spy.optimizer[-1]
    offer = next(item for item in request.offers if item.candidate_id == route)
    cash = offer.expected_cost.cash_usd
    assert cash is not None
    return Decimal(cash)


def _rejected(body: dict[str, Any]) -> dict[str, str]:
    return {item["candidate_id"]: item["code"] for item in body["routing_trace"]["rejected_offers"]}


def test_above_budget_rejected(spy: h.Spy) -> None:
    """Catches: an over-budget shortlist leader still selected."""
    candidates = [
        h.candidate(CHEAP, price=h.priced("1000")),
        h.candidate(MIDDLE, price=h.priced("1")),
        h.candidate(LAST, price=h.priced("1")),
    ]
    with h.client() as client:
        control = client.post("/v1/route?include_routing_trace=true", json=h.route_body(candidates))
        assert control.status_code == 200, control.text
        assert _selected(control.json()) == CHEAP  # shortlist leader without a budget
        budget = float((_cash(spy, CHEAP) + _cash(spy, MIDDLE)) / 2)
        response = client.post(
            "/v1/route?include_routing_trace=true",
            json=h.route_body(candidates, budget=budget, omit_budget=False),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert _selected(body) == MIDDLE
    assert body["routing_trace"]["optimizer_decision"]["selected_candidate_id"] == MIDDLE
    assert _rejected(body)[CHEAP] == "exceeds_hard_spend_budget"


def test_unknown_price_under_budget_rejected(spy: h.Spy) -> None:
    """Catches: unknown price treated as free when a budget is set."""
    candidates = [
        h.candidate(CHEAP),  # no price evidence, not free
        h.candidate(MIDDLE, price=h.priced("1")),
        h.candidate(LAST, price=h.priced("1")),
    ]
    with h.client() as client:
        response = client.post(
            "/v1/route?include_routing_trace=true",
            json=h.route_body(candidates, budget=5, omit_budget=False),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert _selected(body) == MIDDLE
    assert _rejected(body)[CHEAP] == "unknown_price_not_treated_as_free"


def test_exact_budget_qualifies(spy: h.Spy) -> None:
    """Catches: an off-by-one '>=' that rejects an offer costing exactly the budget."""
    candidates = [h.candidate(route, price=h.priced("1")) for route in h.ROUTES]
    with h.client() as client:
        control = client.post("/v1/route", json=h.route_body(candidates))
        assert control.status_code == 200, control.text
        cash = _cash(spy, CHEAP)
        assert Decimal(str(float(cash))) == cash  # the float budget is exactly the cost
        exact = client.post(
            "/v1/route?include_routing_trace=true",
            json=h.route_body(candidates, budget=float(cash), omit_budget=False),
        )
        below = client.post(
            "/v1/route?include_routing_trace=true",
            json=h.route_body(candidates, budget=float(cash) * 0.999, omit_budget=False),
        )
    assert exact.status_code == 200, exact.text
    assert _selected(exact.json()) == CHEAP
    assert CHEAP not in _rejected(exact.json())
    # Control: one hair under the cost and the same offer is rejected.
    assert below.status_code == 400, below.text
    assert _rejected(below.json())[CHEAP] == "exceeds_hard_spend_budget"


def test_no_budget_unknown_price_advisory(spy: h.Spy) -> None:
    """Catches: unknown price becoming a gate without an explicit budget (A25)."""
    candidates = [h.candidate(route) for route in h.ROUTES]  # all unpriced
    with h.client() as client:
        response = client.post(
            "/v1/route?include_routing_trace=true", json=h.route_body(candidates)
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert _selected(body) == CHEAP
    assert "unknown_price_not_treated_as_free" not in _rejected(body).values()


def test_exhausted_pool_fails_closed_without_fallback(spy: h.Spy) -> None:
    """Catches: a fallback route invented when every offer is over budget."""
    candidates = [h.candidate(route, price=h.priced("1000")) for route in h.ROUTES]
    with h.client() as client:
        response = client.post(
            "/v1/route?include_routing_trace=true",
            json=h.route_body(candidates, budget=0.0001, omit_budget=False),
        )
    assert response.status_code == 400, response.text
    body = response.json()
    assert "model" not in body and "provider" not in body
    trace = body["routing_trace"]
    assert trace["optimizer_decision"] == {"outcome": "denied", "selected_candidate_id": None}
    assert _rejected(body) == dict.fromkeys(h.ROUTES, "exceeds_hard_spend_budget")
    assert len(spy.optimizer) == 1, "optimizer re-ran: a second selection pass is a fallback"


def test_missing_execution_path_request_still_400(spy: h.Spy) -> None:
    """Catches: a budget/trace change that weakens execution-path authority."""
    body = h.route_body(_free_candidates())
    body.pop("execution_path_request")
    with h.client() as client:
        plain = client.post("/v1/route", json=body)
        traced = client.post("/v1/route?include_routing_trace=true", json=body)
    assert plain.status_code == 400
    assert traced.status_code == 400
    assert traced.json() == plain.json()
    assert "routing_trace" not in traced.json()
