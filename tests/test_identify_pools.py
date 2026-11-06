"""Per-task pools admit on live, catalog and tool evidence before ordering."""

from datetime import datetime, timedelta, timezone

from verdict.identify_pools import build_supply, build_task_pools
from verdict.metadata.records import (
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.omniroute_evidence import (
    ConnectionHealth,
    LiveCandidate,
    OmniRouteEvidence,
    TrafficEvidence,
)
from verdict.orchestration.health_cache import HealthEntry

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def conn(connection_id="c1", provider="p", usable=True):
    return ConnectionHealth(
        connection_id, provider, usable, None if usable else "inactive", NOW, "active"
    )


def evidence(
    candidates=(),
    *,
    capacities=None,
    cooled=(),
    traffic=None,
    errors=(),
    connections=(conn(),),
    inventory=None,
):
    live = {c.route_id: c for c in candidates}
    return OmniRouteEvidence(
        fetched_at=NOW,
        connections=tuple(connections),
        usable_providers=frozenset(c.provider for c in connections if c.usable),
        proven_by_traffic={},
        cooled_models=frozenset(cooled),
        source_errors=tuple(errors),
        route_capacities=capacities or {},
        inventory_route_ids=frozenset(live if inventory is None else inventory),
        traffic_by_route=traffic or {},
        live_candidates=live,
    )


def live(route, provider="p", reachable=True, reasons=(), conns=("c1",)):
    return LiveCandidate(route, provider, tuple(conns), reachable, tuple(reasons))


def scores(**routes):
    def field(value):
        return ProvenancedField(
            value=value,
            provenance=FieldProvenance(source="openrouter_aa", fetched_at=NOW.isoformat()),
        )

    records = tuple(
        ModelMetadataRecord(
            id=route,
            omniroute_ids=(route,),
            scores=SoftScores(
                aa_coding=field(coding) if coding is not None else None,
                aa_intelligence=field(intelligence) if intelligence is not None else None,
            ),
        )
        for route, (coding, intelligence) in routes.items()
    )
    return MetadataSnapshot(
        schema_version="1", refreshed_at=NOW.isoformat(), sources={}, records=records
    )


def proof(route, *, ok=True, age=timedelta(hours=1)):
    return HealthEntry(
        route_id=route,
        category="ok",
        checked_at=NOW - age,
        until=NOW + timedelta(hours=1),
        consecutive_failures=0,
        chat_ok=True,
        tool_ok=True,
        healthy=True,
        probe_class="agentic",
        agentic_ok=ok,
        agentic_checked_at=NOW - age,
    )


def proofs(*routes):
    return {route: proof(route) for route in routes}


def pools(ev, metadata=None, cache=None):
    return build_task_pools(ev, metadata, None, cache, NOW)


def ids(rows):
    return [row["route_id"] for row in rows]


def test_free_ranks_before_subscription_metered_and_unknown_regardless_of_score():
    routes = {"p/m": "metered", "p/s": "subscription", "p/f": "free", "p/u": "unknown"}
    ev = evidence([live(r) for r in routes], capacities=routes)
    metadata = scores(**{"p/m": (90, None), "p/s": (80, None), "p/f": (10, None)})
    report = pools(ev, metadata, proofs(*routes, "p/u"))
    pool = report["implementation"]
    assert ids(pool["eligible"]) == ["p/f", "p/s", "p/m", "p/u"]
    assert pool["eligible_by_capacity"] == {
        "free": 1,
        "subscription": 1,
        "metered": 1,
        "unknown": 1,
    }
    assert pool["fallback"] == [] and pool["fallback_count"] == 0
    assert report["dispatch_authorized"] is False
    assert report["order"] == "capacity(free,subscription,metered,unknown)->task_rating->route_id"


def test_within_a_tier_stronger_fresh_score_first_and_unknown_last():
    routes = ["p/a", "p/b", "p/c"]
    ev = evidence([live(r) for r in routes], capacities=dict.fromkeys(routes, "free"))
    report = pools(ev, scores(**{"p/a": (50, None), "p/b": (70, None)}), proofs(*routes))
    rows = report["implementation"]["eligible"]
    assert ids(rows) == ["p/b", "p/a", "p/c"]
    assert (rows[0]["basis"], rows[0]["score"], rows[0]["metric"]) == ("score", 70, "aa_coding")
    assert (rows[0]["source"], rows[0]["as_of"]) == ("openrouter_aa", NOW.isoformat())
    assert rows[-1]["basis"] == "unknown" and rows[-1]["score"] is None


def test_no_tool_proof_is_fallback_never_eligible():
    ev = evidence([live("p/a")], capacities={"p/a": "free"})
    for cache in (None, {}):
        pool = pools(ev, cache=cache)["implementation"]
        assert pool["eligible"] == [] and pool["eligible_count"] == 0
        assert ids(pool["fallback"]) == ["p/a"]
        assert pool["fallback"][0]["tool_proof"] == "missing"
        assert pool["fallback"][0]["tool_proof_route"] is None
        assert pool["fallback_by_capacity"]["free"] == 1


def test_opaque_routes_never_enter_a_pool_even_with_tool_proof():
    routes = ["auto/fast", "combo/quality", "p/a"]
    ev = evidence([live(r) for r in routes], capacities=dict.fromkeys(routes, "free"))
    for cache in (None, proofs(*routes)):
        result = pools(ev, cache=cache)
        pool = result["implementation"]
        assert ids(pool["eligible"]) + ids(pool["fallback"]) == ["p/a"]
        assert result["blocked_by_reason"] == {"opaque_route": 2}


def test_stale_or_negative_tool_proof_is_fallback():
    ev = evidence([live("p/stale"), live("p/neg")])
    cache = {"p/stale": proof("p/stale", age=timedelta(days=30)), "p/neg": proof("p/neg", ok=False)}
    pool = pools(ev, cache=cache)["implementation"]
    assert pool["eligible"] == []
    assert sorted(ids(pool["fallback"])) == ["p/neg", "p/stale"]


def test_sibling_backend_model_tool_proof_counts_as_fresh():
    ev = evidence([live("claude/x", provider="claude")], connections=(conn("c1", "claude"),))
    row = pools(ev, cache=proofs("cc/x"))["implementation"]["eligible"][0]
    assert row["route_id"] == "claude/x"
    assert (row["tool_proof"], row["tool_proof_route"]) == ("fresh", "cc/x")


def test_hard_checks_block_with_counted_reasons():
    def traffic(route, status):
        return TrafficEvidence(route, "p", "error", 4, 0, NOW, status)

    ev = evidence(
        [
            live("p/ok"),
            live("p/open", reachable=False, reasons=("breaker_not_closed", "connection_cooldown")),
            live("q/nobody", provider="q"),
            live("p/cooled"),
            live("p/403"),
            live("p/404"),
            live("p/401"),
            live("p/uncatalogued"),
        ],
        cooled=("p/cooled",),
        traffic={r: traffic(r, int(r[2:])) for r in ("p/401", "p/403", "p/404")},
        inventory={"p/ok", "p/open", "q/nobody", "p/cooled", "p/401", "p/403", "p/404", "p/gone"},
    )
    report = pools(ev, cache=proofs("p/ok", "p/cooled", "p/403"))
    assert ids(report["implementation"]["eligible"]) == ["p/ok"]
    assert report["implementation"]["fallback"] == []
    assert report["blocked_by_reason"] == {
        "model_cooling_down": 1,
        "not_in_catalog": 1,
        "not_live:breaker_not_closed,connection_cooldown": 1,
        "not_live:not_a_candidate": 1,
        "provider_unavailable": 1,
        "traffic_auth_or_entitlement_error:401": 1,
        "traffic_auth_or_entitlement_error:403": 1,
        "traffic_auth_or_entitlement_error:404": 1,
    }


def test_candidates_source_error_blocks_every_route_as_liveness_unknown():
    ev = evidence(errors=("candidates",), inventory={"p/a", "p/b"})
    report = pools(ev, cache=proofs("p/a", "p/b"))
    assert report["blocked_by_reason"] == {"liveness_unknown": 2}
    assert all(report[f]["eligible"] == [] == report[f]["fallback"] for f in ("research", "review"))
    supply = build_supply(ev, proofs("p/a"), NOW)
    assert "candidates_source_unavailable" in supply["unknowns"]
    assert supply["complete"] is False


def test_shared_account_aliases_dedupe_to_the_cheaper_first_row():
    capacities = {"cc/x": "free", "claude/x": "subscription"}
    ev = evidence(
        [live("claude/x", provider="claude"), live("cc/x", provider="claude")],
        capacities=capacities,
        connections=(conn("c1", "claude"),),
    )
    pool = pools(ev, cache=proofs("claude/x"))["implementation"]
    assert ids(pool["eligible"]) == ["cc/x"]
    assert pool["eligible"][0]["aliases"] == ["claude/x"]
    assert pool["eligible_count"] == 1
    assert pool["eligible_by_capacity"] == {
        "free": 1,
        "subscription": 0,
        "metered": 0,
        "unknown": 0,
    }


def test_per_task_metric_differs_between_research_and_implementation():
    routes = ["p/a", "p/b"]
    ev = evidence([live(r) for r in routes], capacities=dict.fromkeys(routes, "free"))
    report = pools(ev, scores(**{"p/a": (30, 90), "p/b": (80, 20)}), proofs(*routes))
    assert (report["research"]["metric"], report["implementation"]["metric"]) == (
        "aa_intelligence",
        "aa_coding",
    )
    assert report["research"]["task_family"] == "research"
    assert report["implementation"]["task_family"] == "code_edit"
    assert ids(report["research"]["eligible"]) == ["p/a", "p/b"]
    assert ids(report["implementation"]["eligible"]) == ["p/b", "p/a"]
    assert set(report) >= {"research", "specification", "implementation", "review", "integration"}


def test_supply_names_unknowns_and_counts_layers_apart():
    ev = evidence(
        [
            live("p/a", conns=("c1",)),
            live("p/b", reachable=False, reasons=("excluded",), conns=("c1",)),
            live("cfp/x", provider="cloudflare-playground", conns=("noauth",)),
        ],
        capacities={"p/a": "free", "p/b": "metered"},
        connections=(conn("c1", "p"), conn("g1", "g"), conn("g2", "g", usable=False)),
        inventory={"p/a", "p/b", "cfp/x", "g/model"},
        traffic={"p/a": TrafficEvidence("p/a", "p", "healthy", 2, 2, NOW, 200)},
    )
    supply = build_supply(ev, proofs("p/a"), NOW)
    assert supply["complete"] is False
    assert supply["unknowns"] == sorted(
        [
            "account_identity_not_reported",
            "catalog_routes_without_live_candidate:1",
            "connections_without_live_candidates:g",
            "noauth_candidates:cloudflare-playground",
        ]
    )
    assert supply["totals"] == {
        "catalog_routes": 4,
        "live_candidates": 3,
        "live_reachable": 2,
        "tool_proven": 1,
        "traffic_proven": 0,
        "connections": 3,
        "usable_connections": 2,
        "by_capacity": {"free": 1, "subscription": 0, "metered": 1, "unknown": 1},
    }
    assert supply["layers"] == {
        "catalog": 4,
        "live": 2,
        "tool_proof": 1,
        "task_performance": "not_measured",
    }
    providers = {row["provider"]: row for row in supply["providers"]}
    assert [row["provider"] for row in supply["providers"]] == sorted(providers)
    assert providers["g"]["connections"] == 2 and providers["g"]["usable_connections"] == 1
    assert providers["g"]["live_candidates"] == 0 and providers["g"]["catalog_routes"] == 1
    assert providers["p"]["traffic_routes"] == 1 and providers["p"]["tool_proven_routes"] == 1
    assert providers["cloudflare-playground"]["catalog_routes"] == 1


def test_supply_without_tool_proof_is_unknown():
    supply = build_supply(evidence([live("p/a")]), None, NOW)
    assert "tool_proof_unavailable" in supply["unknowns"]
    assert supply["complete"] is False
