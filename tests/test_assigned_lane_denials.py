"""The assigned lane denies, never re-admits, and says which check dropped the route (5.2, 5.6).

Every denial here asserts, through the real relay, the reason named by the check that dropped the
route, that no proof was minted, that the upstream saw nothing, and that no other route was
selected.  The text classifiers (keyword scanner, worthiness, planner) are replaced by spies that
fail the test if they run: a lane denial must not reach the legacy path.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_assigned_lane import (
    ROUTES,
    TOOLS,
    Wired,
    body_for,
    build_service,
    exclusion_codes,
    metadata_for,
    now,  # noqa: F401  (fixture)
    route_direct,
    store,  # noqa: F401  (fixture)
    verified,
)
from verdict import api, intelligence
from verdict.admission import is_opaque
from verdict.eligibility import EligibilityGate
from verdict.execution_path import ExecutionPathError
from verdict.free_tier_admit import NO_ELIGIBLE_TARGET, admit_free_tier_active
from verdict.orchestration.health_cache import HealthCache, ProbeResult, default_cache_path
from verdict.receipt_store import ReceiptStore


def _boom(name: str) -> Any:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(f"{name} must not run on the assigned lane")

    return fail


def forbid_text_classifiers(monkeypatch: pytest.MonkeyPatch, service: Any) -> None:
    monkeypatch.setattr(intelligence, "scan", _boom("the keyword scanner"))
    monkeypatch.setattr(intelligence, "classify_worthiness", _boom("classify_worthiness"))
    monkeypatch.setattr(service.planner, "plan", _boom("the planner"))


def cool(route: str, when: datetime) -> None:
    """Record a rate-limit failure so the route is negative in the shared health cache."""
    cache = HealthCache(default_cache_path())
    probe = ProbeResult(
        category="rate_limited",
        chat_ok=False,
        tool_ok=False,
        http_status=429,
        failure_scope="route",
        capacity_evidence="http 429",
    )
    cache.merge_and_save(lambda state: state.record(route, probe, when))
    assert cache.lookup(route, when).state == "negative"


# (id, pinned route, build_service options, body extras, accepted reason codes, prep)
DENIALS: list[Any] = [
    pytest.param(
        "ghost/not-in-the-catalog",
        {},
        {},
        {"route_not_in_inventory"},
        None,
        id="absent-from-inventory",
    ),
    pytest.param(
        "auto/best",
        {"extra_catalog": ("auto/best",)},
        {},
        {"opaque_route_disallowed"},
        None,
        id="opaque-auto-in-catalog",
    ),
    pytest.param("auto/best", {}, {}, {"opaque_route_disallowed"}, None, id="opaque-auto-absent"),
    pytest.param(
        "combo/mix",
        {"extra_catalog": ("combo/mix",)},
        {},
        {"opaque_route_disallowed"},
        None,
        id="combo-in-catalog",
    ),
    pytest.param("combo/mix", {}, {}, {"opaque_route_disallowed"}, None, id="combo-absent"),
    pytest.param(
        "blocked/model-b",
        {"extra_catalog": ("blocked/model-b",), "quota_blocked": ("blocked",)},
        {},
        {"quota_exhausted"},
        None,
        id="quota-blocked-provider",
    ),
    pytest.param(
        "dormant/model-d",
        {"extra_catalog": ("dormant/model-d",), "inactive": ("dormant",)},
        {},
        {"provider_inactive"},
        None,
        id="inactive-provider",
    ),
    pytest.param(ROUTES[0], {}, {}, {"route_cooling"}, "cool", id="cooled-route"),
    pytest.param(
        ROUTES[0],
        {"passport_for": ROUTES[1:], "confirm": None},
        {},
        {"no_passport", "confirm_unavailable"},
        "no-confirm",
        id="unknown-health-no-passport-no-confirm",
    ),
    pytest.param(
        ROUTES[0],
        {"gate": EligibilityGate(availability_source=lambda model_id: None)},
        {},
        {"runtime_truth_absent"},
        None,
        id="unknown-health-eligibility-gate",
    ),
    pytest.param(
        ROUTES[2],
        {"metadata": metadata_for(ROUTES, missing=(ROUTES[2],))},
        {},
        {"unmapped"},
        None,
        id="unmapped-no-capability-required",
    ),
    pytest.param(
        ROUTES[3],
        {"metadata": metadata_for(ROUTES, tools={ROUTES[3]: False})},
        {"tools": TOOLS},
        {"capability_mismatch"},
        None,
        id="route-without-tools",
    ),
    pytest.param(
        ROUTES[3],
        {"metadata": metadata_for(ROUTES, tools={ROUTES[3]: None})},
        {"tools": TOOLS},
        {"required_unknown"},
        None,
        id="tools-fact-unknown",
    ),
    pytest.param(
        ROUTES[0],
        {"metadata": False},
        {"tools": TOOLS},
        {"required_unknown"},
        None,
        id="no-metadata-store-with-tools",
    ),
    pytest.param(
        ROUTES[0],
        {"metadata": False},
        {},
        {"required_unknown"},
        None,
        id="no-metadata-store-no-capability",
    ),
    pytest.param(
        ROUTES[4],
        {"metadata": metadata_for(ROUTES, windows={ROUTES[4]: 1500})},
        {},
        {"context_plan_budget_insufficient"},
        None,
        id="window-under-the-plan-minimum",
    ),
    pytest.param(
        ROUTES[1],
        {},
        {"spend_policy": "free_only"},
        {"paid_model_not_allowed"},
        None,
        id="explicit-spend-policy",
    ),
]


@pytest.mark.parametrize(("pinned", "options", "extras", "codes", "prep"), DENIALS)
def test_a_pinned_route_that_fails_a_gate_is_denied_with_the_named_reason_and_no_upstream_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
    pinned: str,
    options: dict[str, Any],
    extras: dict[str, Any],
    codes: set[str],
    prep: str | None,
) -> None:
    service = build_service(now, store, **options)
    if prep == "no-confirm":
        monkeypatch.setattr(service, "_resolve_confirm_transport", lambda endpoint: None)
    wired = Wired(monkeypatch, tmp_path, service)
    forbid_text_classifiers(monkeypatch, service)
    if prep == "cool":
        cool(pinned, now)

    response = wired.post(body_for(pinned, **extras), assigned=pinned)

    assert response.status_code == 503, response.text
    assert wired.upstream == [], "the upstream was contacted for a denied route"
    assert all(proofs == {} for proofs in wired.minted), "a proof was minted for a denied route"
    decision = wired.decisions[-1]
    assert decision.decision == "denied"
    assert decision.admission_proofs == {}
    # The reason is the one the check that dropped the route gave, not a default.
    named = set(exclusion_codes(decision, pinned))
    assert named & codes, (named, codes)
    assert any(code in decision.reason for code in codes), decision.reason
    # No other route is selected.
    assert decision.model == NO_ELIGIBLE_TARGET
    assert decision.alternatives == []
    assert (decision.admit_receipt or {})["admitted"] == []
    assert "worker_assigned_lane" in decision.safety_flags


def test_a_lane_denial_never_selects_another_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    """Whatever drops the pinned route, the five other admitted routes stay unselected."""
    service = build_service(now, store, passport_for=ROUTES[1:], confirm=None)
    monkeypatch.setattr(service, "_resolve_confirm_transport", lambda endpoint: None)
    wired = Wired(monkeypatch, tmp_path, service)
    response = wired.post(body_for(ROUTES[0]), assigned=ROUTES[0])
    assert response.status_code == 503
    decision = wired.decisions[-1]
    assert decision.model == NO_ELIGIBLE_TARGET and decision.alternatives == []
    assert wired.upstream == []
    # The same snapshot serves another route unpinned: only the pin was in question.
    assert route_direct(service, body_for(ROUTES[1]), None).decision == "selected"


# --- explicit protection is honoured and never lowered ----------------------------------------


@pytest.mark.parametrize(
    ("extras", "argument"),
    [
        pytest.param({"protected": True}, "medium", id="protected-field"),
        pytest.param({"criticality": "critical"}, "critical", id="criticality-field"),
        pytest.param({}, "critical", id="criticality-argument"),
        pytest.param({"criticality": "CRITICAL"}, "critical", id="criticality-field-uppercase"),
    ],
)
def test_an_explicitly_protected_request_is_denied_before_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
    extras: dict[str, Any],
    argument: str,
) -> None:
    service = build_service(now, store)
    wired = Wired(monkeypatch, tmp_path, service)
    forbid_text_classifiers(monkeypatch, service)
    monkeypatch.setattr(service, "_offload_free_tier", _boom("the ladder"))

    decision = route_direct(
        service, body_for(ROUTES[0], **extras), verified(ROUTES[0]), criticality=argument
    )

    assert decision.decision == "denied"
    assert decision.protected is True
    assert "worker_assignment_protected_denied" in decision.safety_flags
    assert "protected work is never offloaded" in decision.reason
    assert decision.admission_proofs == {} and decision.model == NO_ELIGIBLE_TARGET
    assert wired.upstream == [] and wired.minted == []


def test_a_protected_request_through_the_relay_makes_no_upstream_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    service = build_service(now, store)
    wired = Wired(monkeypatch, tmp_path, service)
    monkeypatch.setattr(service, "_offload_free_tier", _boom("the ladder"))
    response = wired.post(body_for(ROUTES[0], protected=True), assigned=ROUTES[0])
    assert response.status_code == 503
    assert wired.upstream == [] and wired.minted == []
    assert wired.decisions[-1].protected is True


# --- no live admission: deny, never the legacy path --------------------------------------------


def test_a_lane_that_cannot_get_live_admission_denies_and_never_reaches_the_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    service = build_service(now, store, with_snapshot=False, providers={})
    forbid_text_classifiers(monkeypatch, service)
    monkeypatch.setattr(service, "static_catalog", _boom("the static catalog"))
    decision = route_direct(service, body_for(ROUTES[0]), verified(ROUTES[0]))
    assert decision.decision == "denied"
    assert decision.model == NO_ELIGIBLE_TARGET
    assert "worker_assigned_lane" in decision.safety_flags
    assert decision.admission_proofs == {}


def test_an_unusable_spend_policy_is_a_denial_not_a_server_error(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    service = build_service(now, store)
    forbid_text_classifiers(monkeypatch, service)
    decision = route_direct(
        service, body_for(ROUTES[0], spend_policy="no-such-policy"), verified(ROUTES[0])
    )
    assert decision.decision == "denied" and decision.admission_proofs == {}


# --- production profile and the authority requirement ------------------------------------------


@pytest.mark.parametrize("how", ["production-profile", "env-switch"])
def test_the_production_profile_and_the_authority_switch_never_reach_the_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
    how: str,
) -> None:
    if how == "production-profile":
        service = build_service(now, store, profile="production")
    else:
        monkeypatch.setenv("VERDICT_REQUIRE_EXECUTION_PATH", "1")
        service = build_service(now, store)
    wired = Wired(monkeypatch, tmp_path, service)
    monkeypatch.setattr(service, "_offload_free_tier", _boom("the ladder"))
    forbid_text_classifiers(monkeypatch, service)
    response = wired.post(body_for(ROUTES[0]), assigned=ROUTES[0])
    assert response.status_code == 400, response.text
    assert wired.upstream == [] and wired.minted == []
    with pytest.raises(ExecutionPathError):
        route_direct(service, body_for(ROUTES[0]), verified(ROUTES[0]))


# --- 5.6: the guarantees the three admission tests do not exercise -----------------------------


def _random_world(rng: random.Random) -> dict[str, Any]:
    routes = tuple(f"p{i}/m{i}" for i in range(rng.randint(3, 8)))
    providers = [r.split("/")[0] for r in routes]
    return {
        "routes": routes,
        "inactive": tuple(p for p in providers if rng.random() < 0.2),
        "quota_blocked": tuple(p for p in providers if rng.random() < 0.2),
        "passport_for": tuple(r for r in routes if rng.random() < 0.8),
        "cooled": tuple(r for r in routes if rng.random() < 0.2),
        "extra_catalog": tuple(x for x in ("auto/best", "combo/mix") if rng.random() < 0.5),
    }


@pytest.mark.parametrize("seed", range(8))
def test_the_lane_admitted_set_is_a_subset_of_the_pinned_route_and_the_unpinned_active_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
    seed: int,
) -> None:
    """Nothing is re-admitted: for every route in a generated snapshot (seeded, no new dependency)."""
    world = _random_world(random.Random(1313 + seed))
    routes = world["routes"]
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health.json"))
    for route in world["cooled"]:
        cool(route, now)
    service = build_service(
        now,
        store,
        routes=routes,
        extra_catalog=world["extra_catalog"],
        inactive=world["inactive"],
        quota_blocked=world["quota_blocked"],
        passport_for=world["passport_for"],
        metadata=metadata_for(routes),
        top_k=3,
    )
    snapshot = service.admit_snapshot
    cache = HealthCache(default_cache_path())
    active_not_cooled = {
        route
        for route in admit_free_tier_active(snapshot).admitted
        if cache.lookup(route, now).state != "negative"
    }
    probes = [*routes, *world["extra_catalog"], "ghost/unknown"]
    for pinned in probes:
        decision = route_direct(service, body_for(pinned), verified(pinned))
        admitted = set((decision.admit_receipt or {}).get("admitted", []))
        assert admitted <= {pinned} & active_not_cooled, (pinned, admitted)
        # Liveness, so the property cannot pass by denying everything: a route that is active, not
        # cooled, has a fresh passport and real metadata is admitted.
        if pinned in active_not_cooled and pinned in world["passport_for"]:
            assert decision.decision == "selected", (pinned, decision.reason)
        if decision.decision == "selected":
            assert decision.model == pinned and decision.alternatives == []
            assert pinned in active_not_cooled and not is_opaque(pinned)
        else:
            assert decision.model == NO_ELIGIBLE_TARGET and decision.admission_proofs == {}


def test_the_generated_worlds_include_both_admitted_and_denied_routes(
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health.json"))
    selected = denied = 0
    for seed in range(8):
        world = _random_world(random.Random(1313 + seed))
        service = build_service(
            now,
            store,
            routes=world["routes"],
            extra_catalog=world["extra_catalog"],
            inactive=world["inactive"],
            quota_blocked=world["quota_blocked"],
            passport_for=world["passport_for"],
        )
        for pinned in world["routes"]:
            decision = route_direct(service, body_for(pinned), verified(pinned))
            selected += decision.decision == "selected"
            denied += decision.decision == "denied"
    assert selected > 0 and denied > 0


def test_the_relay_admitted_set_check_still_denies_an_assigned_route_outside_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    """``VERDICT_RELAY_ADMISSION_CACHE`` narrows an assigned request exactly as any request."""

    class Admitted:
        def __contains__(self, route_id: object) -> bool:
            return route_id != ROUTES[0]

    service = build_service(now, store)
    wired = Wired(monkeypatch, tmp_path, service)
    monkeypatch.setattr(api, "relay_admission_provider", lambda: Admitted())
    outside = wired.post(body_for(ROUTES[0]), assigned=ROUTES[0])
    assert outside.status_code == 503, outside.text
    assert wired.upstream == []
    inside = wired.post(body_for(ROUTES[1]), assigned=ROUTES[1])
    assert inside.status_code == 200, inside.text
    assert [row["body"]["model"] for row in wired.upstream] == [ROUTES[1]]


def test_cooling_window_is_not_a_default_a_fresh_cache_does_not_cool_anything(
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "fresh.json"))
    service = build_service(now, store)
    assert route_direct(service, body_for(ROUTES[0]), verified(ROUTES[0])).decision == "selected"
    cool(ROUTES[0], now - timedelta(seconds=1))
    assert route_direct(service, body_for(ROUTES[0]), verified(ROUTES[0])).decision == "denied"


def test_the_lane_never_runs_the_text_classifiers_when_it_admits_either(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    service = build_service(now, store)
    forbid_text_classifiers(monkeypatch, service)
    monkeypatch.setattr(intelligence, "apply_best_of_admitted", _boom("the best-of ranking"))
    monkeypatch.setattr(intelligence, "build_cheap_path_context_pack", _boom("the pack builder"))
    text = "update the payment webhook; implement and test the api; rotate the credential"
    decision = route_direct(service, body_for(ROUTES[5], text), verified(ROUTES[5]))
    assert decision.decision == "selected", decision.reason
    assert decision.model == ROUTES[5]


def test_a_decision_is_denied_when_the_receipt_holds_more_than_the_signed_route(
    now: datetime,  # noqa: F811
    store: ReceiptStore,  # noqa: F811
) -> None:
    """Defence in depth behind the narrowing: the lane never serves another route.

    The narrowing leaves one route, so no request reaches this state; the decision is still
    built to refuse it, so a future change to the ladder cannot silently serve a different route.
    """
    service = build_service(now, store)
    snapshot = service.admit_snapshot
    receipt = admit_free_tier_active(snapshot)
    assert len(receipt.admitted) > 1
    decision = service._decision_from_admit(
        "do the work",
        3,
        False,
        "",
        receipt,
        receipt.as_eligibility_result(snapshot),
        endpoint=None,
        live=False,
        task_class="unclassified",
        context={},
        assignment=verified(ROUTES[0]),
    )
    assert decision.decision == "denied"
    assert decision.model == NO_ELIGIBLE_TARGET and decision.admission_proofs == {}
    assert decision.alternatives == []
