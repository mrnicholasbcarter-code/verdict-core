"""Consented identify screening reuses the three-turn agentic probe, not chat proof."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict import identify
from verdict.metadata.records import (
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.omniroute_evidence import OmniRouteEvidence, TrafficEvidence
from verdict.orchestration.health_cache import (
    CATEGORY_PAYMENT,
    HealthCache,
    HealthEntry,
    ProbeResult,
)
from verdict.prove_at_rest import (
    AGENTIC_PROBE_FILE,
    AGENTIC_TOOL_EDIT,
    AGENTIC_TOOL_READ,
    ProbeExchange,
    _inband_refusal_exchange,
)

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("no_gateway_network")


def snapshot():
    ids = ["p/weak", "p/strong", "q/model", "alias/strong"]
    return OmniRouteEvidence(
        fetched_at=NOW,
        connections=(),
        usable_providers=frozenset({"p", "q"}),
        proven_by_traffic={
            r: TrafficEvidence(r, "q" if r.startswith("q/") else "p", "healthy", 2, 2, NOW)
            for r in ids
        },
        cooled_models=frozenset(),
        source_errors=(),
        route_identities={r: ("q" if r.startswith("q/") else "p", r.split("/", 1)[1]) for r in ids},
        route_capacities={r: "free" if r == "p/weak" else "subscription" for r in ids},
    )


def ratings():
    records = tuple(
        ModelMetadataRecord(
            id=route,
            omniroute_ids=(route,),
            scores=SoftScores(
                aa_coding=ProvenancedField(
                    value=score,
                    provenance=FieldProvenance(source="openrouter_aa", fetched_at=NOW.isoformat()),
                )
            ),
        )
        for route, score in [
            ("p/strong", 90),
            ("alias/strong", 90),
            ("q/model", 70),
            ("p/weak", 10),
        ]
    )
    return MetadataSnapshot(
        schema_version="1", refreshed_at=NOW.isoformat(), sources={}, records=records
    )


def passing(calls):
    def send(route, payload, timeout):
        calls.append((route, payload))
        turn = sum(m.get("role") == "user" for m in payload["messages"]) - 1
        script = [
            (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
            (
                AGENTIC_TOOL_EDIT,
                {"path": AGENTIC_PROBE_FILE, "old_text": "line two", "new_text": "LINE TWO"},
            ),
            (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
        ]
        name, args = script[turn]
        return ProbeExchange(
            http_status=200,
            ok=True,
            reported_model=route,
            latency_ms=2,
            response_body={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"t{turn}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    return send


def screen(tmp_path, calls, **kwargs):
    options = dict(
        metadata=ratings(),
        cache=HealthCache(tmp_path / "health.json"),
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=1,
        max_routes=2,
        clock=lambda: NOW,
        capacities=("subscription",),
    )
    options.update(kwargs)
    return identify.screen_models(snapshot(), **options)


def test_screen_without_consent_sends_nothing(tmp_path):
    calls = []
    with pytest.raises(ValueError, match="live_probe_consent_required"):
        screen(tmp_path, calls, allow_live_probe=False)
    assert calls == []
    assert not (tmp_path / "health.json").exists()


def test_screen_caps_strongest_first_alias_dedupe_and_subscription_proof(tmp_path):
    calls = []
    report = screen(tmp_path, calls, capacities=("free", "subscription"))
    checked = [r for r in report["routes"] if r["turns_sent"]]
    assert [r["route_id"] for r in checked] == ["alias/strong", "q/model"]
    assert checked[0]["basis"] == "score"
    assert checked[0]["score"] == 90
    assert checked[0]["provider"] == "p"
    assert all(r["result"] == "pass" and r["turns_sent"] == 3 for r in checked)
    assert report["routes_checked"] == report["passed"] == 2
    assert report["model_requests_sent"] == len(calls) == 6
    assert report["stopped_reason"] == "route_cap"
    assert {r["result"] for r in report["routes"]} >= {"skipped_alias", "skipped_cap"}
    cache = HealthCache(tmp_path / "health.json")
    entry = cache.entry("alias/strong")
    assert entry.agentic_ok and entry.identity == "verified"
    assert entry.agentic_checked_at == NOW
    assert cache.entry("p/strong") is None
    assert any(m["role"] == "tool" for m in calls[1][1]["messages"])
    assert cache.entry("q/model").agentic_checked_at == NOW


def test_total_and_per_provider_caps_are_independent(tmp_path):
    calls = []
    report = screen(tmp_path, calls, max_routes=1, max_per_provider=5)
    assert len(calls) == 3
    assert report["routes_checked"] == 1
    assert report["stopped_reason"] == "route_cap"
    other = tmp_path / "other"
    report = screen(
        other, [], max_routes=5, max_per_provider=1, capacities=("free", "subscription")
    )
    assert report["routes_checked"] == 2
    assert report["stopped_reason"] == "provider_cap"


def test_capacity_is_only_an_explicit_filter(tmp_path):
    calls = []
    report = screen(tmp_path, calls, capacities=("free",))
    assert [r[0] for r in calls] == ["p/weak"] * 3
    assert report["routes_checked"] == 1
    assert report["model_requests_sent"] == 3


def test_fresh_agentic_is_skipped_but_chat_does_not_qualify(tmp_path):
    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "alias/strong",
        ProbeResult(
            category="ok",
            chat_ok=True,
            tool_ok=True,
            probe_class="agentic",
            agentic_ok=True,
            identity="verified",
        ),
        NOW - timedelta(hours=1),
    )
    cache.record("q/model", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    cache.save()
    calls = []
    report = screen(tmp_path, calls, cache=cache)
    assert report["routes"][0]["result"] == "skipped_fresh"
    assert [r[0] for r in calls] == ["q/model"] * 3
    assert cache.entry("alias/strong").agentic_checked_at == NOW - timedelta(hours=1)
    assert cache.entry("q/model").agentic_ok


@pytest.mark.parametrize("limit_turn", [1, 2, 3])
def test_429_writes_no_capability_and_stops_provider_only(tmp_path, limit_turn):
    calls = []
    good = passing(calls)

    def send(route, payload, timeout):
        if route == "alias/strong" and len(calls) + 1 == limit_turn:
            calls.append((route, payload))
            return ProbeExchange(http_status=429, ok=False, retry_after_seconds=60)
        return good(route, payload, timeout)

    report = screen(tmp_path, calls, transport=send, max_per_provider=5, max_routes=5)
    assert [r[0] for r in calls] == ["alias/strong"] * limit_turn + ["q/model"] * 3
    assert report["model_requests_sent"] == limit_turn + 3
    assert report["routes"][0]["result"] == "rate_limited"
    assert (
        next(r for r in report["routes"] if r["route_id"] == "p/strong")["result"]
        == "skipped_alias"
    )
    cache = HealthCache(tmp_path / "health.json")
    assert cache.entry("alias/strong") is None
    assert cache.entry("q/model").agentic_ok


@pytest.mark.parametrize("kind", ["chat", "identity", "missing_identity"])
def test_chat_only_or_wrong_identity_never_mints_agentic_proof(tmp_path, kind):
    calls = []
    good = passing(calls)

    def send(route, payload, timeout):
        ex = good(route, payload, timeout)
        if kind == "chat":
            return replace(
                ex, response_body={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
            )
        return replace(ex, reported_model="" if kind == "missing_identity" else "wrong/model")

    report = screen(tmp_path, calls, transport=send, max_routes=1)
    assert report["passed"] == 0
    assert report["routes"][0]["result"] == ("fail" if kind == "chat" else "identity_mismatch")
    entry = HealthCache(tmp_path / "health.json").entry("alias/strong")
    assert entry is not None and not entry.agentic_ok


def test_current_negative_and_cooldown_are_skipped(tmp_path):
    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "alias/strong", ProbeResult(category="upstream", chat_ok=False, tool_ok=False), NOW
    )
    cache.record(
        "q/other", ProbeResult(category="authentication", chat_ok=False, tool_ok=False), NOW
    )
    cache.save()
    calls = []
    report = screen(tmp_path, calls, cache=cache, capacities=("free", "subscription"))
    assert [r[0] for r in calls] == ["p/weak"] * 3
    assert (
        next(r for r in report["routes"] if r["route_id"] == "alias/strong")["result"]
        == "skipped_negative"
    )
    assert (
        next(r for r in report["routes"] if r["route_id"] == "q/model")["result"]
        == "skipped_cooldown"
    )


def test_newer_negative_during_conversation_wins_and_original_clock_is_kept(tmp_path):
    calls = []
    good = passing(calls)
    path = tmp_path / "health.json"

    def send(route, payload, timeout):
        ex = good(route, payload, timeout)
        if len(calls) == 3:
            other = HealthCache(path)
            other.record(
                route,
                ProbeResult(category="upstream", chat_ok=False, tool_ok=False),
                NOW + timedelta(seconds=1),
            )
            other.record(
                "unrelated/route", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW
            )
            other.save()
        return ex

    report = screen(tmp_path, calls, transport=send, max_routes=1)
    assert report["routes"][0]["result"] == "skipped_newer_evidence"
    assert report["passed"] == 0
    entry = HealthCache(path).entry("alias/strong")
    assert entry.category == "upstream" and not entry.agentic_ok
    assert HealthCache(path).entry("unrelated/route").tool_ok


def test_evidence_preserves_authoritative_alias_identity_and_capacity(billing_overlay_subscription):
    # CP2 removed oauth → SUBSCRIPTION; write the operator overlay so the
    # fake "alpha/oauth" connection classifies as subscription.
    billing_overlay_subscription([("alpha", "oauth")])

    from test_identify import payloads

    from verdict.omniroute_evidence import parse_omniroute_evidence

    data = payloads()
    data["providers"]["connections"][0]["authType"] = "oauth"
    evidence = parse_omniroute_evidence(**data, now=NOW)
    assert evidence.route_identities["a/best"] == ("alpha", "best")
    assert evidence.route_identities["alias/best"] == ("alpha", "best")
    assert evidence.route_capacities["a/best"] == "subscription"
    # Contradictory usable connections cannot establish a capacity class.
    other = dict(data["providers"]["connections"][0], id="second", authType="apikey")
    data["providers"]["connections"].append(other)
    live = dict(data["resilience"]["connections"][0], id="second")
    data["resilience"]["connections"].append(live)
    evidence = parse_omniroute_evidence(**data, now=NOW)
    assert evidence.route_capacities["a/best"] == "unknown"


def test_partial_deadline_counts_sent_turn_but_writes_no_proof(tmp_path):
    calls = []
    tick = [0.0]
    good = passing(calls)

    def send(route, payload, timeout):
        ex = good(route, payload, timeout)
        tick[0] = 601.0
        return ex

    report = screen(tmp_path, calls, transport=send, monotonic=lambda: tick[0], max_routes=1)
    assert report["model_requests_sent"] == 1
    assert report["passed"] == 0
    assert report["stopped_reason"] == "wall_cap"
    assert HealthCache(tmp_path / "health.json").entry("alias/strong") is None


@pytest.mark.parametrize("status", [401, 403])
def test_screen_auth_failure_cannot_flush_over_newer_positive(tmp_path, status):
    calls = []
    good = passing(calls)
    path = tmp_path / "health.json"
    newer = NOW + timedelta(minutes=5)

    def send(route, payload, timeout):
        if route == "alias/strong":
            calls.append((route, payload))
            return ProbeExchange(http_status=status, ok=False)
        if route == "q/model":
            other = HealthCache(path)
            other.record(
                "alias/strong",
                ProbeResult(
                    category="ok",
                    chat_ok=True,
                    tool_ok=True,
                    probe_class="agentic",
                    agentic_ok=True,
                    identity="verified",
                ),
                newer,
            )
            other.save()
        return good(route, payload, timeout)

    report = screen(tmp_path, calls, transport=send, max_per_provider=5, max_routes=5)
    entry = HealthCache(path).entry("alias/strong")
    assert entry.agentic_ok and entry.checked_at == newer
    assert report["routes"][0]["result"] == "auth_failed"
    assert next(r for r in report["routes"] if r["route_id"] == "q/model")["result"] == "pass"


@pytest.mark.parametrize("status", [401, 403])
def test_every_screen_route_fails_auth_and_report_matches_cache(tmp_path, status):
    calls = []

    def send(route, payload, timeout):
        calls.append((route, payload))
        return ProbeExchange(http_status=status, ok=False)

    report = screen(tmp_path, calls, transport=send, max_per_provider=5, max_routes=5)
    cache = HealthCache(tmp_path / "health.json")
    attempted = [row for row in report["routes"] if row["turns_sent"]]
    assert attempted
    assert report["passed"] == 0
    assert report["model_requests_sent"] == len(calls) == len(attempted)
    for row in report["routes"]:
        entry = cache.entry(row["route_id"])
        if row["turns_sent"]:
            assert row["result"] == "auth_failed"
            assert entry.category == ("authentication" if status == 401 else "permission")
            assert entry.checked_at == NOW
            assert not entry.agentic_ok and entry.http_status == status
        else:
            assert row["result"].startswith("skipped_")
            assert entry is None


def test_empty_bucket_is_not_reported_as_cooldown(tmp_path):
    cache = HealthCache(tmp_path / "health.json")
    assert cache.reserve_bucket("q", NOW, amount=10) is not None
    calls = []
    report = screen(tmp_path, calls, cache=cache, providers=("q",))
    assert calls == []
    assert report["routes"][0]["result"] == "skipped_bucket"


@pytest.mark.parametrize(
    "status,detail,scope,cooled",
    [
        (401, "bad credentials", None, True),
        (403, "account access denied", None, True),
        (403, "model alias/strong is not permitted", "route", False),
    ],
)
def test_screen_auth_keeps_existing_failure_scope_and_pool_cooldown(
    tmp_path, status, detail, scope, cooled
):
    from verdict.prove_at_rest import _cooled_pools

    cache = HealthCache(tmp_path / "health.json")
    calls = []

    def send(route, payload, timeout):
        calls.append((route, payload))
        return ProbeExchange(http_status=status, ok=False, detail=detail)

    report = screen(tmp_path, calls, cache=cache, transport=send, max_routes=1)
    assert report["routes"][0]["result"] == "auth_failed"
    entry = HealthCache(tmp_path / "health.json").entry("alias/strong")
    assert entry.failure_scope == scope
    assert entry.pool == "alias"
    assert ("alias" in _cooled_pools(cache, NOW)) is cooled


def test_strongest_rating_screens_before_lower_rated_free(tmp_path):
    calls = []
    report = screen(tmp_path, calls, capacities=("free", "subscription"))
    checked = [r for r in report["routes"] if r["turns_sent"]]
    assert [r["route_id"] for r in checked] == ["alias/strong", "q/model"]
    assert [r[0] for r in calls] == ["alias/strong"] * 3 + ["q/model"] * 3
    assert report["passed"] == 2


def test_community_screen_joins_exact_routes_and_skips_billing_aliases(tmp_path):
    from verdict.discovery.sources.community import parse_fcm
    from verdict.omniroute_evidence import LiveCandidate

    roster = parse_fcm(
        'export const p = [["org/good", "Good"], ["bad", "Bad"],'
        ' ["metered", "Metered"], ["missing", "Missing"]];'
    )
    ids = ["p/org/good", "p/bad", "alias/bad", "p/metered", "p/not-in-roster"]
    evidence = replace(
        snapshot(),
        proven_by_traffic={},
        inventory_route_ids=frozenset(ids),
        inventory_rows={rid: {"owned_by": "p"} for rid in ids},
        route_identities={rid: ("p", rid.partition("/")[2]) for rid in ids},
        live_candidates={rid: LiveCandidate(rid, "p", ("connection",), True) for rid in ids},
        route_capacities={rid: "metered" if rid == "p/metered" else "unknown" for rid in ids},
    )
    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "alias/bad",
        ProbeResult(chat_ok=False, tool_ok=False, category=CATEGORY_PAYMENT, http_status=402),
        NOW - timedelta(hours=1),
    )
    cache.save()
    calls = []
    report = identify.screen_models(
        evidence,
        metadata=None,
        cache=cache,
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=10,
        max_routes=10,
        capacities=("free", "unknown"),
        clock=lambda: NOW,
        roster=roster,
    )
    assert {rid for rid, _ in calls} == {"p/org/good"}
    assert {r["model_id"]: r["result"] for r in report["roster"]} == {
        "p/bad": "skipped_billing_refusal",
        "p/metered": "skipped_capacity",
        "p/missing": "model_not_registered",
        "p/org/good": "pass",
    }
    assert (
        next(r for r in report["routes"] if r["route_id"] == "p/org/good")["capacity"] == "unknown"
    )


def test_roster_screen_checks_unknown_capacity_and_expired_refusals_by_default(tmp_path):
    """Catches unknown-capacity routes, or refusals past their window, never being screened."""
    from verdict.discovery.sources.community import parse_fcm
    from verdict.omniroute_evidence import LiveCandidate

    roster = parse_fcm('export const p = [["unknown", "Unknown"], ["was-refused", "Refused"]];')
    ids = ["p/unknown", "p/was-refused"]
    evidence = replace(
        snapshot(),
        proven_by_traffic={},
        inventory_route_ids=frozenset(ids),
        inventory_rows={rid: {"owned_by": "p"} for rid in ids},
        route_identities={rid: ("p", rid.partition("/")[2]) for rid in ids},
        live_candidates={rid: LiveCandidate(rid, "p", ("connection",), True) for rid in ids},
        route_capacities=dict.fromkeys(ids, "unknown"),
    )
    cache = HealthCache(tmp_path / "health.json")
    refusal = ProbeResult(chat_ok=False, tool_ok=False, category=CATEGORY_PAYMENT, http_status=402)
    cache.record("p/was-refused", refusal, NOW - timedelta(days=3))
    cache.save()
    calls = []
    report = identify.screen_models(
        evidence,
        metadata=None,
        cache=cache,
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=10,
        max_routes=10,
        clock=lambda: NOW,
        roster=roster,
    )
    assert {rid for rid, _ in calls} == set(ids)
    assert report["roster_counts"] == {"pass": 2}


def test_roster_counts_a_keyless_inventory_provider_as_configured():
    """Catches a keyless provider's unregistered models reported as an unconfigured provider."""
    from verdict.discovery.sources.community import parse_fcm

    roster = parse_fcm(
        'export const keyless = [["served", "Served"], ["missing", "Missing"]];'
        '\nexport const absent = [["other", "Other"]];'
    )
    evidence = replace(
        snapshot(),
        usable_providers=frozenset(),
        proven_by_traffic={},
        inventory_route_ids=frozenset({"keyless/served"}),
        inventory_rows={"keyless/served": {"owned_by": "keyless"}},
        route_identities={},
    )
    report = identify._roster_report(identify._roster_routes(roster, evidence), [], evidence)
    assert {r["model_id"]: r["result"] for r in report["roster"]} == {
        "keyless/served": "not_reachable",
        "keyless/missing": "model_not_registered",
        "absent/other": "not_configured",
    }
    assert report["roster_coverage"]["configured_provider_matches"] == 2


def test_assumed_free_screen_records_capacity_evidence(tmp_path):
    evidence = replace(
        snapshot(),
        route_capacities={"p/weak": "free"},
        route_capacity_evidence={"p/weak": "assumed_free_no_paid_evidence"},
    )
    cache = HealthCache(tmp_path / "health.json")
    report = identify.screen_models(
        evidence,
        metadata=ratings(),
        cache=cache,
        transport=passing([]),
        allow_live_probe=True,
        max_per_provider=1,
        max_routes=1,
        capacities=("free",),
        clock=lambda: NOW,
    )
    row = next(r for r in report["routes"] if r["route_id"] == "p/weak")
    assert row["result"] == "pass"
    assert row["capacity_evidence"] == "assumed_free_no_paid_evidence"
    assert cache.entry("p/weak").capacity_evidence == "assumed_free_no_paid_evidence"


def tiered_snapshot():
    """Free routes with known tier 1, tier 3 and unknown tier, all strongest-rated."""
    ids = ["p/claude-sonnet-4.5", "p/gemma-4-26b-a4b-it", "q/zzqx-blob"]
    rows = {
        r: {"id": r, "capabilities": {"tool_calling": True}, "context_length": 200_000} for r in ids
    }
    return OmniRouteEvidence(
        fetched_at=NOW,
        connections=(),
        usable_providers=frozenset({"p", "q"}),
        proven_by_traffic={
            r: TrafficEvidence(r, r.split("/", 1)[0], "healthy", 2, 2, NOW) for r in ids
        },
        cooled_models=frozenset(),
        source_errors=(),
        route_identities={r: (r.split("/", 1)[0], r.split("/", 1)[1]) for r in ids},
        route_capacities=dict.fromkeys(ids, "free"),
        inventory_rows=rows,
    )


def test_tier_cap_never_probes_insufficient_or_unknown_tier(tmp_path):
    """Catches the cert preflight spending its cap on routes the planner rejects."""
    calls = []
    report = identify.screen_models(
        tiered_snapshot(),
        metadata=None,
        cache=HealthCache(tmp_path / "health.json"),
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=2,
        max_routes=1,
        clock=lambda: NOW,
        capacities=("free",),
        max_capability_tier=2,
    )
    assert {r[0] for r in calls} == {"p/claude-sonnet-4.5"}
    by_id = {r["route_id"]: r for r in report["routes"]}
    assert by_id["p/gemma-4-26b-a4b-it"]["result"] == "skipped_capability_tier"
    assert by_id["q/zzqx-blob"]["result"] == "skipped_unknown_tier"
    assert by_id["p/claude-sonnet-4.5"]["capability_tier"] == 1
    assert by_id["p/claude-sonnet-4.5"]["supports_tools"] is True
    assert by_id["p/claude-sonnet-4.5"]["context_window"] == 200_000
    # Skipped routes do not consume the route cap.
    assert report["routes_checked"] == report["passed"] == 1
    assert report["stopped_reason"] == "complete"


def test_without_tier_cap_screen_order_and_membership_are_unchanged(tmp_path):
    """Catches the tier filter leaking into screens that did not ask for it."""
    calls = []
    report = identify.screen_models(
        tiered_snapshot(),
        metadata=None,
        cache=HealthCache(tmp_path / "health.json"),
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=2,
        max_routes=3,
        clock=lambda: NOW,
        capacities=("free",),
    )
    assert report["routes_checked"] == 3
    assert not any(r["result"].startswith("skipped_") for r in report["routes"])


def test_tier_cap_outside_known_tiers_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="max_capability_tier"):
        screen(tmp_path, [], max_capability_tier=4)


def inventory_snapshot(specs):
    """Free routes from ``(route_id, tier, context_window, tools)``; ``None`` leaves a fact unknown."""
    rows = {}
    for route_id, tier, context, tools in specs:
        row = {"id": route_id}
        if tier is not None:
            row["capability_tier"] = tier
        if context is not None:
            row["context_length"] = context
        if tools is not None:
            row["capabilities"] = {"tool_calling": tools}
        rows[route_id] = row
    ids = list(rows)
    prefixes = frozenset(r.split("/", 1)[0] for r in ids)
    return OmniRouteEvidence(
        fetched_at=NOW,
        connections=(),
        usable_providers=prefixes,
        proven_by_traffic={
            r: TrafficEvidence(r, r.split("/", 1)[0], "healthy", 2, 2, NOW) for r in ids
        },
        cooled_models=frozenset(),
        source_errors=(),
        route_identities={r: (r.split("/", 1)[0], r.split("/", 1)[1]) for r in ids},
        route_capacities=dict.fromkeys(ids, "free"),
        inventory_rows=rows,
    )


def screen_inventory(tmp_path, calls, specs, **kwargs):
    options = dict(
        metadata=None,
        cache=HealthCache(tmp_path / "health.json"),
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=2,
        max_routes=8,
        clock=lambda: NOW,
        capacities=("free",),
    )
    options.update(kwargs)
    return identify.screen_models(inventory_snapshot(specs), **options)


def probed(calls):
    """Route ids in first-probe order."""
    return list(dict.fromkeys(route for route, _ in calls))


# Two aliased pools (google-antigravity, openrouter-free), routes below and
# without the planner floor, and more candidates than the caps admit.
GOLDEN_INVENTORY = [
    ("agy/alpha", 1, 200_000, True),
    ("antigravity/beta", 1, 200_000, True),
    ("cerebras/zeta", 2, 131_072, True),
    ("cf/small-context", 2, 24_000, True),
    ("cf/unknown-context", 2, None, True),
    ("groq/tools-unknown", 1, 200_000, None),
    ("groq/tools-false", 1, 200_000, False),
    ("kc/gamma:free", 2, 131_072, True),
    ("nvidia/epsilon", 1, 128_000, True),
    ("openrouter/delta:free", 2, 131_072, True),
    ("zz/unknown-tier", None, 200_000, True),
]
GOLDEN_PATH = (
    Path(__file__).parent / "fixtures" / "identify_screen" / "no_new_flags_main_59cfc45d.json"
)
PLANNER_FLOOR = dict(max_capability_tier=2, min_context_tokens=100_000, require_tools=True)


def results(report):
    return {row["route_id"]: row["result"] for row in report["routes"]}


def test_route_below_context_floor_is_never_probed_and_takes_no_slot(tmp_path):
    """Catches #1151: cap slots spent on routes below the caller's context floor."""
    calls = []
    report = screen_inventory(
        tmp_path,
        calls,
        [
            ("cf/context-24k", 2, 24_000, True),
            ("cf/context-80k", 2, 80_000, True),
            ("groq/context-200k", 1, 200_000, True),
        ],
        max_routes=1,
        min_context_tokens=100_000,
    )
    assert probed(calls) == ["groq/context-200k"]
    assert results(report) == {
        "cf/context-24k": "skipped_task_floor",
        "cf/context-80k": "skipped_task_floor",
        "groq/context-200k": "pass",
    }
    assert report["routes_checked"] == report["passed"] == 1
    assert report["stopped_reason"] == "complete"


def test_unknown_context_and_unknown_tools_are_below_the_floor(tmp_path):
    """Catches an unknown fact being treated as meeting the floor."""
    specs = [
        ("a/context-unknown", 1, None, True),
        ("b/tools-unknown", 1, 200_000, None),
        ("c/tools-false", 1, 200_000, False),
        ("d/meets-floor", 1, 200_000, True),
    ]
    calls = []
    report = screen_inventory(
        tmp_path / "both", calls, specs, min_context_tokens=100_000, require_tools=True
    )
    assert probed(calls) == ["d/meets-floor"]
    assert results(report) == {
        "a/context-unknown": "skipped_task_floor",
        "b/tools-unknown": "skipped_task_floor",
        "c/tools-false": "skipped_task_floor",
        "d/meets-floor": "pass",
    }
    # Each half of the floor applies only when asked for.
    calls = []
    screen_inventory(tmp_path / "tools", calls, specs, require_tools=True)
    assert probed(calls) == ["a/context-unknown", "d/meets-floor"]
    calls = []
    screen_inventory(tmp_path / "context", calls, specs, min_context_tokens=100_000)
    assert probed(calls) == ["b/tools-unknown", "c/tools-false", "d/meets-floor"]


@pytest.mark.parametrize(
    "first,second,pool",
    [
        ("agy/alpha", "antigravity/beta", "google-antigravity"),
        ("kc/alpha:free", "openrouter/beta:free", "openrouter-free"),
    ],
)
def test_pool_cap_counts_aliased_prefixes_as_one_pool(tmp_path, first, second, pool):
    """Catches two probes landing in one quota pool while another pool goes unproven."""
    calls = []
    report = screen_inventory(
        tmp_path,
        calls,
        [(first, 1, 200_000, True), (second, 1, 200_000, True), ("zz/other", 1, 200_000, True)],
        max_routes=2,
        max_per_pool=1,
    )
    assert probed(calls) == [first, "zz/other"]
    assert results(report) == {first: "pass", second: "skipped_pool_cap", "zz/other": "pass"}
    assert {r["route_id"]: r["backend_pool"] for r in report["routes"]} == {
        first: pool,
        second: pool,
        "zz/other": "zz",
    }
    assert report["routes_checked"] == 2
    assert report["stopped_reason"] == "pool_cap"


def test_pool_cap_covers_every_pool_before_any_pool_gets_a_second_probe(tmp_path):
    """Catches the total cap being spent inside the first pools in rating order."""
    specs = [
        ("a/one", 1, 200_000, True),
        ("a/two", 1, 200_000, True),
        ("b/one", 1, 200_000, True),
        ("b/two", 1, 200_000, True),
        ("c/one", 1, 200_000, True),
    ]
    calls = []
    report = screen_inventory(tmp_path / "three", calls, specs, max_routes=3, max_per_pool=2)
    assert probed(calls) == ["a/one", "b/one", "c/one"]
    assert report["stopped_reason"] == "route_cap"
    # Rows keep the screen's own order whatever order the probes ran in.
    assert [r["route_id"] for r in report["routes"]] == [s[0] for s in specs]
    calls = []
    screen_inventory(tmp_path / "five", calls, specs, max_routes=5, max_per_pool=2)
    assert probed(calls) == ["a/one", "b/one", "c/one", "a/two", "b/two"]


def test_skipped_route_takes_no_pool_slot(tmp_path):
    """Catches a floor skip using up its pool's first-round probe."""
    calls = []
    report = screen_inventory(
        tmp_path,
        calls,
        [
            ("a/one", 1, 200_000, True),
            ("a/two", 1, 200_000, True),
            ("b/context-24k", 1, 24_000, True),
            ("b/two", 1, 200_000, True),
        ],
        max_routes=3,
        max_per_pool=2,
        min_context_tokens=100_000,
    )
    assert probed(calls) == ["a/one", "b/two", "a/two"]
    assert results(report)["b/context-24k"] == "skipped_task_floor"


def test_without_new_options_the_screen_matches_main(tmp_path):
    """Catches the floor or the pool cap leaking into screens that did not ask for them.

    The golden file is this screen's JSON as produced by main at 59cfc45d.
    """
    calls = []
    report = screen_inventory(tmp_path, calls, GOLDEN_INVENTORY, max_per_provider=1, max_routes=4)
    assert json.dumps(report, sort_keys=True, indent=1, default=str) + "\n" == (
        GOLDEN_PATH.read_text()
    )
    assert probed(calls) == ["agy/alpha", "antigravity/beta", "cerebras/zeta", "cf/small-context"]


def test_new_options_only_remove_routes_from_the_probed_set(tmp_path):
    """Catches an option adding a candidate or probing a route the plain screen would not."""
    plain_calls, calls = [], []
    plain = screen_inventory(tmp_path / "plain", plain_calls, GOLDEN_INVENTORY, max_routes=99)
    report = screen_inventory(
        tmp_path / "floor", calls, GOLDEN_INVENTORY, max_routes=99, max_per_pool=1, **PLANNER_FLOOR
    )
    assert [r["route_id"] for r in report["routes"]] == [r["route_id"] for r in plain["routes"]]
    assert probed(calls) == ["agy/alpha", "cerebras/zeta", "kc/gamma:free", "nvidia/epsilon"]
    assert set(probed(calls)) < set(probed(plain_calls))
    assert results(report) == {
        "agy/alpha": "pass",
        "antigravity/beta": "skipped_pool_cap",
        "cerebras/zeta": "pass",
        "cf/small-context": "skipped_task_floor",
        "cf/unknown-context": "skipped_task_floor",
        "groq/tools-false": "skipped_task_floor",
        "groq/tools-unknown": "skipped_task_floor",
        "kc/gamma:free": "pass",
        "nvidia/epsilon": "pass",
        "openrouter/delta:free": "skipped_pool_cap",
        "zz/unknown-tier": "skipped_unknown_tier",
    }


@pytest.mark.parametrize("option", [{"max_per_pool": 0}, {"min_context_tokens": 0}])
def test_non_positive_floor_or_pool_cap_is_rejected(tmp_path, option):
    """Catches a zero cap or floor being read as 'not set'."""
    calls = []
    with pytest.raises(ValueError, match="invalid_" + next(iter(option))):
        screen(tmp_path, calls, **option)
    assert calls == []


@pytest.mark.parametrize(
    "option", [{"max_per_pool": 1}, {"min_context_tokens": 100_000}, {"require_tools": True}]
)
def test_floor_and_pool_cap_require_screen_mode(option):
    """Catches a passive report silently ignoring a screen-only option."""
    with pytest.raises(ValueError, match="requires --screen"):
        identify.identify_report(**option)


def _refusing(calls, refusals):
    """Pass every route except those in ``refusals``, which return their exchange."""
    good = passing(calls)

    def send(route, payload, timeout):
        if route in refusals:
            calls.append((route, payload))
            return refusals[route]
        return good(route, payload, timeout)

    return send


INBAND_CREDIT = {
    "usage": {"prompt_tokens": 10, "completion_tokens": 0},
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "Insufficient credits. Please add credits to your account to continue.",
            }
        }
    ],
}


@pytest.mark.parametrize(
    "refusal",
    [
        ProbeExchange(http_status=401, ok=False, detail="bad credentials"),
        ProbeExchange(http_status=402, ok=False, detail="payment required"),
    ],
    ids=["auth_401", "payment_402"],
)
def test_pool_seen_refusing_takes_no_more_cap_slots(tmp_path, refusal):
    """Catches cert-fix10: slots spent on pools that already refused auth or payment.

    The refusing pool's second route is skipped without a probe and the cap
    reaches a later good pool instead.
    In-band credit refusals are route-scoped (failure_scope="route") and do not
    mark the pool; they are covered by test_route_scoped_402_does_not_skip_its_pool.
    """
    from verdict.prove_at_rest import is_inband_billing_refusal

    assert is_inband_billing_refusal(INBAND_CREDIT)
    calls = []
    specs = [
        ("bad/one", 1, 200_000, True),
        ("bad/two", 1, 200_000, True),
        ("good/one", 1, 200_000, True),
    ]
    report = screen_inventory(
        tmp_path,
        calls,
        specs,
        transport=_refusing(calls, {"bad/one": refusal, "bad/two": refusal}),
        max_routes=2,
        max_per_pool=2,
    )
    assert probed(calls) == ["bad/one", "good/one"]
    assert results(report)["bad/two"] == "skipped_pool_refused"
    assert results(report)["good/one"] == "pass"
    assert report["routes_checked"] == 2


def test_route_scoped_403_does_not_skip_its_pool(tmp_path):
    """Catches one model's 403 being read as the whole pool refusing."""
    calls = []
    refusal = ProbeExchange(http_status=403, ok=False, detail="model bad/one is not permitted")
    report = screen_inventory(
        tmp_path,
        calls,
        [("bad/one", 1, 200_000, True), ("bad/two", 1, 200_000, True)],
        transport=_refusing(calls, {"bad/one": refusal}),
        max_routes=2,
        max_per_pool=2,
    )
    assert probed(calls) == ["bad/one", "bad/two"]
    assert results(report)["bad/two"] == "pass"


def test_named_http_402_does_not_skip_its_sibling(tmp_path):
    calls = []
    refusal = ProbeExchange(http_status=402, ok=False, detail="model bad/one requires credits")
    report = screen_inventory(
        tmp_path,
        calls,
        [("bad/one", 1, 200_000, True), ("bad/two", 1, 200_000, True)],
        transport=_refusing(calls, {"bad/one": refusal}),
        max_routes=2,
        max_per_pool=2,
    )
    assert probed(calls) == ["bad/one", "bad/two"]
    assert results(report)["bad/two"] == "pass"
    entry = HealthCache(tmp_path / "health.json").entry("bad/one")
    assert entry is not None and entry.failure_scope == "route"
    assert entry.capacity_evidence == "billing_402_metered"


def test_route_scoped_402_does_not_skip_its_pool(tmp_path):
    """Route-scoped 402 (in-band credit refusal) must not mark the pool.

    Catches OCR finding on PR #1172: a paid model's in-band payment refusal was
    recorded with failure_scope='route' but _account_refusal() ignored that scope
    and blocked free siblings in the same pool. Payment refusals must respect
    failure_scope the same way auth refusals do.
    """
    calls = []
    # _inband_refusal_exchange → ProbeExchange(http_status=200, CATEGORY_PAYMENT)
    # The prober records this with failure_scope="route" (route's own pricing).
    refusal = _inband_refusal_exchange(2, "bad/one")
    report = screen_inventory(
        tmp_path,
        calls,
        [("bad/one", 1, 200_000, True), ("bad/two", 1, 200_000, True)],
        transport=_refusing(calls, {"bad/one": refusal}),
        max_routes=2,
        max_per_pool=2,
    )
    assert probed(calls) == ["bad/one", "bad/two"]
    assert results(report)["bad/two"] == "pass"


@pytest.mark.parametrize(
    "failure_scope,expected",
    [
        ("route", False),  # route-scoped: free sibling in the same pool is not blocked
        (None, True),  # unscoped: pool is marked refusing (account-wide payment failure)
        ("pool", True),  # pool-scoped: pool is marked refusing
        ("account", True),  # account-scoped: pool is marked refusing
    ],
)
def test_account_refusal_payment_honours_failure_scope(failure_scope, expected):
    """_account_refusal() respects failure_scope for payment the same way it does for auth."""
    entry = HealthEntry(
        route_id="bad/one",
        category=CATEGORY_PAYMENT,
        checked_at=NOW,
        until=NOW,
        consecutive_failures=1,
        chat_ok=False,
        tool_ok=False,
        http_status=402,
        failure_scope=failure_scope,
    )
    row: dict[str, object] = {"turns_sent": 1}
    assert identify._account_refusal(row, entry) == expected


def test_rescreen_requested_bypasses_skipped_fresh_and_probes(tmp_path: object) -> None:
    """Bug 1b: a route with a pending rescreen request is probed despite fresh agentic proof.

    An unrequested fresh route in the same run is still skipped_fresh.
    """
    cache = HealthCache(tmp_path / "health.json")  # type: ignore[operator]
    # Both subscription routes have fresh agentic proof (1 hour old — well within 24-h TTL).
    for route in ("alias/strong", "q/model"):
        cache.record(
            route,
            ProbeResult(
                category="ok",
                chat_ok=True,
                tool_ok=True,
                probe_class="agentic",
                agentic_ok=True,
                identity="verified",
            ),
            NOW - timedelta(hours=1),
        )
    cache.save()
    # Only alias/strong has a pending rescreen request.
    cache.request_rescreen(["alias/strong"], now=NOW)

    calls: list[object] = []
    report = screen(tmp_path, calls, cache=cache)  # type: ignore[arg-type]

    routes_by_id = {r["route_id"]: r for r in report["routes"]}

    # alias/strong: fresh proof + rescreen request → must be probed, not skipped_fresh.
    alias_row = routes_by_id["alias/strong"]
    assert alias_row["result"] not in {"skipped_fresh", "skipped_negative"}, (
        f"alias/strong has a rescreen request and should be probed; got {alias_row['result']!r}"
    )
    assert alias_row["turns_sent"] == 3, "full 3-turn agentic probe must run"

    # q/model: fresh proof, no rescreen request → still skipped_fresh.
    q_row = routes_by_id.get("q/model")
    if q_row is not None:
        assert q_row["result"] == "skipped_fresh", (
            f"q/model has no rescreen request and should be skipped_fresh; got {q_row['result']!r}"
        )

    # The hint must be consumed after the probe sends its first turn.
    reloaded = HealthCache(tmp_path / "health.json")  # type: ignore[operator]
    assert "alias/strong" not in reloaded.rescreen_requests()
