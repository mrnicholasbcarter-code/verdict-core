"""A25 invariance with a hard spend budget and the routing trace (BOD-432).

With matched authorization and proof and several eligible candidates, a
label-only or price-only change that keeps every candidate's budget
eligibility must not move the admitted set, the ordered shortlist, the
optimizer outcome/choice or the public selection. Assertions read structured
``routing_trace`` fields, never reason prose. Real app and service; only
gateway reads are faked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.helpers import public_route_harness as h

CHEAP, MIDDLE, LAST = "kr/claude-haiku-5", "kr/claude-sonnet-5", "kr/claude-opus-5"
BUDGET = 5


def _baseline() -> list[dict[str, Any]]:
    return [h.candidate(route, price=h.priced("1")) for route in h.ROUTES]


def _mutate(candidates: list[dict[str, Any]], route: str, **changes: Any) -> list[dict[str, Any]]:
    return [{**item, **changes} if item["route_id"] == route else item for item in candidates]


def _observe(client: Any, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post(
        "/v1/route?include_routing_trace=true",
        json=h.route_body(candidates, budget=BUDGET, omit_budget=False),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    trace = body["routing_trace"]
    return {
        "admitted": set(trace["admitted_candidate_ids"]),
        "shortlist": trace["shortlist_candidate_ids"],
        "optimizer": trace["optimizer_decision"],
        "rejected": trace["rejected_offers"],
        "selection": f"{body['provider']}/{body['model']}",
    }


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return h.install(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "labels",
    [
        {"is_free": True},
        {"is_cheap": True},
        {"is_paid": True},
        {"is_frontier": True},
        {"strategy": "direct_cheap", "is_paid": True, "is_frontier": True},
    ],
)
def test_label_only_mutation(svc: Any, labels: dict[str, Any]) -> None:
    """Catches: a cost/tier label on another candidate reordering or re-admitting."""
    baseline = _baseline()
    with h.client() as client:
        before = _observe(client, baseline)
        after = _observe(client, _mutate(baseline, MIDDLE, **labels))
    assert len(before["admitted"]) == len(h.ROUTES)  # several eligible candidates
    assert before["optimizer"]["outcome"] == "selected"
    assert after == before


@pytest.mark.parametrize("target", [MIDDLE, CHEAP])
@pytest.mark.parametrize("rate", ["0.5", "2", "20"])
def test_price_only_mutation(svc: Any, target: str, rate: str) -> None:
    """Catches: price ordering competing with the trusted shortlist under a budget."""
    baseline = _baseline()
    with h.client() as client:
        before = _observe(client, baseline)
        after = _observe(client, _mutate(baseline, target, price=h.priced(rate)))
    assert before["selection"] == before["optimizer"]["selected_candidate_id"]
    assert after == before
