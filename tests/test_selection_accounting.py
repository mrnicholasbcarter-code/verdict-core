"""Selection accounting is evidence, never a second admission authority."""

import pytest

from tests.test_identify_rank import NOW, _conn, _ladder_row, _snapshot
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))


def test_free_only_never_lists_paid_attempt():
    from verdict.orchestration.eligibility import selection_accounting

    block = selection_accounting(
        [{"route_id": "sub/paid", "capacity_order": 1, "admitted": True}], attempted=["sub/paid"]
    )
    assert all(not values for values in block.values())


def test_selector_emits_accounting_without_claiming_a_dispatch(tmp_path):
    rows = [
        _ladder_row("free/good", pricing={"input": 0}, tier=0),
        _ladder_row("free/weak", pricing={"input": 0}, tier=3),
        _ladder_row("free/no-tools", pricing={"input": 0}, tier=0),
    ]
    rows[-1]["capabilities"] = {}
    selector = EligibilityLadder(
        rows,
        [_conn("free", auth="apikey", free=True)],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=_snapshot(),
    )
    chosen, verdicts = selector.select(
        TaskRequirements(frontier_worthy=True, max_capability_tier=0), now=NOW
    )
    assert chosen.route_id == "free/good"
    block = chosen.rank_components["selection_accounting"]
    assert block == {
        "eligible_free_attempted": [],
        "known_unavailable": [],
        "known_unavailable_reasons": [],
        "unqualified_supply": ["free/no-tools", "free/weak"],
        "unqualified_reasons": [
            {"route_id": "free/no-tools", "reason": "missing_capability:tools"},
            {"route_id": "free/weak", "reason": "insufficient_capability"},
        ],
        "eligible_unattempted_authority_expired": [],
    }
    assert len(verdicts) == 3


def test_receipt_uses_real_dispatches_and_preserves_selector_rejections(tmp_path):
    from verdict.orchestration.candidate_builder import build_candidates
    from verdict.orchestration.receipt import EventLog, _node_record

    selector = EligibilityLadder(
        [
            _ladder_row("free/good", pricing={"input": 0}, tier=0),
            _ladder_row("free/weak", pricing={"input": 0}, tier=3),
        ],
        [_conn("free", auth="apikey", free=True)],
        lambda _: HealthResult(True, "ok"),
        tmp_path / "state.json",
        metadata=_snapshot(),
    )
    _, verdicts = selector.select(
        TaskRequirements(frontier_worthy=True, max_capability_tier=0), now=NOW
    )
    candidates, _, _ = build_candidates(verdicts, "free/good")
    log = EventLog(tmp_path / "events.jsonl")
    log.emit(
        "eligibility",
        "n",
        candidates=candidates,
        selection_accounting=verdicts[0].rank_components["selection_accounting"],
    )
    log.emit("selection", "n", route_id="free/good", capacity_class="free")
    before = _node_record("n", "implement", log.read())
    assert before["selection_accounting"]["eligible_free_attempted"] == []
    log.emit("dispatch", "n", route_id="free/good", capacity_class="free", attempt=1)
    after = _node_record("n", "implement", log.read())["selection_accounting"]
    assert after["eligible_free_attempted"] == ["free/good"]
    assert after["unqualified_supply"] == ["free/weak"]


@pytest.mark.parametrize("expired", [False, True])
def test_classes_are_disjoint_sorted_and_authority_requires_evidence(expired):
    from verdict.orchestration.eligibility import selection_accounting

    rows = [
        {"route_id": "free/sent", "capacity_order": 0, "admitted": True},
        {
            "route_id": "free/sent",
            "capacity_order": 0,
            "failed_stage": "AVAILABLE",
            "reason": "cooldown:route",
        },
        {"route_id": "free/quota", "reason": "quota_exhausted", "admitted": False},
        {"route_id": "free/missing", "reason": "no_agentic_probe", "failed_stage": "TASK_ELIGIBLE"},
        {"route_id": "free/left", "admitted": True, "capacity_order": 0},
        {"route_id": "sub/left", "admitted": True, "capacity_order": 1},
        {"route_id": "auto/no", "admitted": True},
    ]
    block = selection_accounting(rows, attempted=["free/sent"], authority_expired=expired)
    assert block["eligible_free_attempted"] == ["free/sent"]
    assert block["known_unavailable"] == ["free/quota"]
    assert block["unqualified_supply"] == ["free/missing"]
    assert block["eligible_unattempted_authority_expired"] == (
        ["free/left", "sub/left"] if expired else []
    )


def test_buffered_relay_reports_actual_free_sends(monkeypatch, tmp_path):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tests.relay_proof_helpers import with_relay_proofs
    from tests.test_api_relay_context_length import ContextLengthTransport
    from tests.test_proxy import FixedIntelligence
    from verdict import api
    from verdict.models import RoutingDecision
    from verdict.proxy import UpstreamProxy

    ids = ["free-a", "free-b", "free-c", "free-d", "sub-a"]

    class Intelligence(FixedIntelligence):
        async def route(self, task, criticality="medium", context=None, *, request_id=None):
            decision = with_relay_proofs(
                RoutingDecision(
                    model=ids[0],
                    provider="omniroute",
                    tier=2,
                    reason="fixture",
                    alternatives=ids[1:3],
                    safety_flags=["subscription_fallback_policy"],
                    candidate_states=[
                        {
                            "route_id": route,
                            "model_id": route,
                            "admitted": True,
                            "capacity_order": 0,
                        }
                        for route in ids[:3]
                    ],
                ),
                context,
            )
            return replace(
                decision,
                alternatives=ids[1:],
                candidate_states=[
                    {
                        "route_id": route,
                        "model_id": route,
                        "admitted": True,
                        "capacity_order": int(route == "sub-a"),
                    }
                    for route in ids
                ],
            )

    transport = ContextLengthTransport([(503, '{"error":"unavailable"}')] * 3)
    outcomes = []
    original = api._finish_evidence

    def capture(evidence, outcome, key):
        outcomes.append(outcome.to_dict())
        return original(evidence, outcome, key)

    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    monkeypatch.setattr(api, "_build_intelligence", lambda: Intelligence())
    monkeypatch.setattr(api, "_finish_evidence", capture)
    monkeypatch.setattr(
        api, "_build_proxy", lambda: UpstreamProxy("http://upstream.test/v1", transport=transport)
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "fix"}]},
            headers={"idempotency-key": "accounting"},
        )
    assert response.status_code == 503
    details = outcomes[-1]["details"]
    assert details["termination_reason"] == "budget_exhausted"
    assert details["selection_accounting"]["eligible_free_attempted"] == ids[:3]
    assert details["selection_accounting"]["eligible_unattempted_authority_expired"] == ids[3:]
