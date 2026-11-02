from datetime import timedelta

from tests.test_explain_selection import NOW, _conn, _Probe, _row
from verdict.omniroute_rankings import OmniRouteRankings
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder


def test_local_generic_gateway_is_not_probed(monkeypatch):
    monkeypatch.delenv("VERDICT_GATEWAY_KIND", raising=False)
    calls = []
    client = OmniRouteRankings(
        "http://127.0.0.1:4000",
        None,
        fetch_json=lambda *args: calls.append(args) or {"rankings": []},
    )
    assert client.fetch("coding").omission == "generic_gateway"
    assert calls == []


def test_explicit_remote_omniroute_preserves_actual_api_context():
    usage = {"requests": 4, "lastRequestAt": None, "windowHours": 24}
    reliability = {"state": "unknown", "connections": [], "usage": usage}
    client = OmniRouteRankings(
        "https://gateway.example",
        None,
        gateway_kind="omniroute",
        clock=lambda: NOW,
        fetch_json=lambda *args: {
            "rankings": [
                {"id": "p", "topModel": {"modelId": "m", "score": 0.9}, "reliability": reliability}
            ]
        },
    )
    result = client.fetch("coding")
    assert result.source == "omniroute:/api/free-provider-rankings"
    assert result.received_at == NOW.isoformat()
    assert result.source_observed_at is None
    assert result.rows[0].reliability_context == reliability
    assert result.rows[0].usage_context == usage
    assert result.rows[0].reliability is None
    assert result.is_fresh(NOW)
    assert not result.is_fresh(NOW + timedelta(days=2))


def test_explicit_kind_from_environment_is_production_reachable(monkeypatch):
    monkeypatch.setenv("VERDICT_GATEWAY_KIND", "omniroute")
    calls = []
    client = OmniRouteRankings(
        "https://remote.example",
        None,
        fetch_json=lambda *args: calls.append(args) or {"rankings": []},
    )
    client.fetch("coding")
    assert len(calls) == 1


def test_stale_local_observation_preserves_complete_baseline_order(tmp_path):
    req = TaskRequirements(
        max_capability_tier=3, min_context_tokens=16_000, required_capabilities=frozenset()
    )
    rows = [_row("p/model-a"), _row("p/model-b"), _row("p/model-c")]
    baseline = EligibilityLadder(rows, [_conn("p")], _Probe(), tmp_path / "base.json")
    stale = EligibilityLadder(
        rows,
        [_conn("p")],
        _Probe(),
        tmp_path / "stale.json",
        rankings={"p/model-c": 1, "p/model-b": 2, "p/model-a": 3},
        rankings_received_at=(NOW - timedelta(days=2)).isoformat(),
    )
    base_chosen, base_verdicts = baseline.select(req, now=NOW)
    stale_chosen, stale_verdicts = stale.select(req, now=NOW)
    assert base_chosen is not None and stale_chosen is not None
    assert stale_chosen.route_id == base_chosen.route_id
    assert [v.route_id for v in stale_verdicts] == [v.route_id for v in base_verdicts]
    assert stale_chosen.rank_components["advisory_omission"] == "stale"
    _, base_candidates = baseline._assess_all(req, NOW)
    _, stale_candidates = stale._assess_all(req, NOW)
    assert [a.route_id for a in sorted(stale_candidates, key=stale._rank_key)] == [
        a.route_id for a in sorted(base_candidates, key=baseline._rank_key)
    ]


def test_ranking_provenance_reaches_selected_route(tmp_path):
    provenance = {
        "source": "omniroute:/api/free-provider-rankings",
        "received_at": NOW.isoformat(),
        "source_observed_at": None,
        "reliability": {"state": "unknown"},
        "usage": None,
    }
    ladder = EligibilityLadder(
        [_row("p/model-a")],
        [_conn("p")],
        _Probe(),
        tmp_path / "ranked.json",
        rankings={"p/model-a": 1},
        rankings_provenance={"p/model-a": provenance},
        rankings_received_at=NOW.isoformat(),
    )
    selected, _ = ladder.select(
        TaskRequirements(
            max_capability_tier=3, min_context_tokens=16_000, required_capabilities=frozenset()
        ),
        now=NOW,
    )
    assert selected is not None
    assert selected.rank_components["advisory_provenance"] == provenance
