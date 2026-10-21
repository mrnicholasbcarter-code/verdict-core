"""Consented identify screening reuses the three-turn agentic probe, not chat proof."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.prove_at_rest import (
    AGENTIC_PROBE_FILE,
    AGENTIC_TOOL_EDIT,
    AGENTIC_TOOL_READ,
    ProbeExchange,
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
    assert [r["route_id"] for r in checked] == ["p/weak", "q/model"]
    assert checked[0]["basis"] == "score"
    assert checked[0]["score"] == 10
    assert checked[0]["provider"] == "p"
    assert all(r["result"] == "pass" and r["turns_sent"] == 3 for r in checked)
    assert report["routes_checked"] == report["passed"] == 2
    assert report["model_requests_sent"] == len(calls) == 6
    assert report["stopped_reason"] == "route_cap"
    assert {r["result"] for r in report["routes"]} >= {"skipped_alias", "skipped_cap"}
    cache = HealthCache(tmp_path / "health.json")
    entry = cache.entry("p/weak")
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


def test_evidence_preserves_authoritative_alias_identity_and_capacity():
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


def test_free_screens_before_higher_rated_subscription(tmp_path):
    calls = []
    report = screen(tmp_path, calls, capacities=("free", "subscription"))
    checked = [r for r in report["routes"] if r["turns_sent"]]
    assert [r["route_id"] for r in checked] == ["p/weak", "q/model"]
    assert [r[0] for r in calls] == ["p/weak"] * 3 + ["q/model"] * 3
    assert report["passed"] == 2


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
