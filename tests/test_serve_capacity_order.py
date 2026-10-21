"""Tier-first serve ordering, billing evidence, and read-only choose regressions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from tests import test_live_candidate_shortlist as live
from verdict.candidate_pool import build_candidate_pool
from verdict.free_tier_admit import snapshot_from_payloads


def _snapshot():
    ids = (live.STRONG, live.FAST, live.NEW, live.NO_TOOLS)
    return snapshot_from_payloads(
        catalog={"data": [{"id": rid, "owned_by": rid.split("/")[0]} for rid in ids]},
        free_tier={
            "perModel": [
                {
                    "modelId": rid.split("/", 1)[1],
                    "provider": rid.split("/")[0],
                    "freeType": "keyless",
                }
                for rid in ids
            ]
        },
        providers={
            "connections": [
                {
                    "provider": rid.split("/")[0],
                    "isActive": True,
                    "testStatus": "active",
                    "authType": "oauth" if rid in (live.STRONG, live.NEW) else "apikey",
                }
                for rid in ids
            ]
        },
    )


def _service():
    service = live._service([], top_k=2)
    service.admit_snapshot = _snapshot()
    return service


def _context(request):
    return {**live._context(request), "spend_policy": "paid_allowed"}


def test_qualified_free_below_score_topk_is_selected():
    service = _service()
    request = live._request(live.STRONG, live.NEW, live.FAST)
    ctx = _context(request)
    prepared = service._prepare_execution_path_request("fix code", "low", ctx, request)
    assert prepared.pool_receipt.shortlist[0].route_id == live.FAST
    assert prepared.pool_receipt.shortlist[0].capacity_order == 0
    assert live.FAST in {entry.route_id for entry in prepared.pool_receipt.shortlist}
    decision = asyncio.run(service.route("fix code", criticality="low", context=ctx))
    assert decision.model == live.FAST.split("/", 1)[1]


def test_no_free_qualified_selects_subscription():
    service = _service()
    request = live._request(live.STRONG, live.NO_TOOLS)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert prepared.pool_receipt.shortlist[0].route_id == live.STRONG
    assert live.NO_TOOLS in prepared.hard_excluded_ids
    decision = asyncio.run(service.route("fix code", criticality="low", context=_context(request)))
    assert decision.model == live.STRONG.split("/", 1)[1]


def test_capacity_only_orders_survivors_and_keeps_same_tier_behavior():
    from verdict.capacity import CapacityClass

    ids = (live.STRONG, live.NEW, live.FAST)
    kwargs = dict(task="fix code", metadata=live._metadata(), top_k=2)
    original = build_candidate_pool(ids, **kwargs)
    same_tier = build_candidate_pool(ids, capacity=dict.fromkeys(ids, CapacityClass.FREE), **kwargs)
    assert [x.route_id for x in original.shortlist] == [x.route_id for x in same_tier.shortlist]
    tiered = build_candidate_pool(
        ids,
        capacity={
            live.FAST: CapacityClass.FREE,
            live.STRONG: CapacityClass.SUBSCRIPTION,
            live.NEW: CapacityClass.SUBSCRIPTION,
        },
        **kwargs,
    )
    assert tiered.shortlist[0].route_id == live.FAST
    assert tiered.hard_drops == original.hard_drops
    filtered = build_candidate_pool(
        ids,
        policy_allowed=(live.STRONG,),
        capacity=dict.fromkeys(ids, CapacityClass.FREE),
        **kwargs,
    )
    assert [x.route_id for x in filtered.shortlist] == [live.STRONG]
    assert {x.route_id: x.reason for x in filtered.hard_drops} == {
        live.NEW: "policy_excluded",
        live.FAST: "policy_excluded",
    }


def test_billing_override_changes_both_selection_surfaces(monkeypatch, tmp_path):
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.health_cache import HealthCache, default_cache_path
    from verdict.relay import relay_failure_feedback
    from verdict.subagent_selection import HealthResult

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))
    from datetime import datetime, timezone

    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    cache = HealthCache(default_cache_path())
    result = relay_failure_feedback(cache, route_id=live.STRONG, status_code=402, now=live.NOW)
    assert result.recorded
    entry = cache.entry(live.STRONG)
    assert entry.category == "payment_required"
    assert entry.capacity_evidence == "billing_402_metered"
    assert entry.failure_scope == "route"
    assert entry.until - entry.checked_at >= timedelta(hours=6)
    assert cache.entry(live.FAST) is None
    ladder = EligibilityLadder(
        [{"id": live.STRONG, "owned_by": "quality"}],
        [{"provider": "quality", "authType": "oauth", "isActive": True}],
        lambda _: HealthResult(False, "not_probed"),
        tmp_path / "state.json",
        health_cache=cache,
    )
    assert ladder.capacity_class(live.STRONG).value == "metered"
    service = _service()
    request = live._request(live.STRONG, live.FAST)
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _context(request), request
    )
    assert live.STRONG not in {row.route_id for row in prepared.pool_receipt.shortlist}
    assert any(
        row.route_id == live.STRONG and row.reason == "metered_fallback_disallowed"
        for row in prepared.pool_receipt.hard_drops
    )


def test_profile_obvious_coding_text_uses_coding_metric():
    from verdict.task_profile import profile_task

    task = "edit a python function and run its test"
    profile = profile_task(task)
    assert profile.task_family.startswith("code")
    pool = build_candidate_pool((live.STRONG,), task_profile=profile, metadata=live._metadata())
    assert pool.shortlist[0].eval_prior_metric == "aa_coding"


def test_shared_capacity_module_is_single_source():
    import verdict.candidate_pool as pool
    import verdict.capacity as capacity
    import verdict.orchestration.eligibility as eligibility

    assert capacity.CAPACITY_ORDER is pool.CAPACITY_ORDER
    assert capacity.CAPACITY_ORDER is eligibility._CAPACITY_ORDER
    assert capacity.classify_route_capacity is eligibility._classify_capacity


def test_explicit_free_only_excludes_paid_snapshot_routes():
    service = _service()
    snapshot = service.admit_snapshot
    service.admit_snapshot = replace(
        snapshot,
        catalog=tuple(
            replace(row, price_known=True, input_cost=1.0, output_cost=1.0)
            if row.identity_id == live.STRONG
            else row
            for row in snapshot.catalog
        ),
        free_tier=tuple(row for row in snapshot.free_tier if row.provider != "quality"),
    )
    request = live._request(live.STRONG, live.FAST)
    ctx = {**_context(request), "spend_policy": "free_only"}
    prepared = service._prepare_execution_path_request("fix code", "low", ctx, request)
    assert live.STRONG in prepared.hard_excluded_ids
    assert all(entry.route_id != live.STRONG for entry in prepared.pool_receipt.shortlist)


def test_diversity_never_swaps_only_free_slot_for_subscription():
    from verdict.capacity import CapacityClass

    pool = build_candidate_pool(
        (live.STRONG, live.FAST),
        task="fix code",
        metadata=live._metadata(),
        top_k=1,
        capacity={live.STRONG: CapacityClass.SUBSCRIPTION, live.FAST: CapacityClass.FREE},
    )
    assert [entry.route_id for entry in pool.shortlist] == [live.FAST]


def test_snapshot_preserves_specific_connection_capacity_evidence():
    from verdict.free_tier_admit import parse_provider_connections

    rows = parse_provider_connections(
        {
            "connections": [
                {
                    "provider": "p",
                    "isActive": True,
                    "authType": "oauth",
                    "providerSpecificData": {"plan": "free", "importFreeModelsOnly": True},
                },
                {
                    "provider": "p",
                    "isActive": True,
                    "authType": "oauth",
                    "providerSpecificData": {"plan": "max"},
                },
            ]
        }
    )
    assert len(rows) == 2
    assert rows[0].auth_type == "oauth"
    assert rows[0].plan_label == "free"
    assert rows[0].import_free_only is True
    assert rows[1].plan_label == "max"
    assert rows[1].import_free_only is False
