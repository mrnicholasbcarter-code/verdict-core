"""The scheduled screen skips routes that cannot pass and learns which providers do.

Covers the screen queue of the ``evidence-driven-screening`` change (decisions 2, 3
and 4): a route without declared tool support is skipped, a provider that never
passes is backed off, and the queue is ordered by the provider's record. Nothing
here sends a live request: every transport is a script.
"""

import json
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
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.orchestration.verified_refresh import _JobLock
from verdict.prove_at_rest import (
    AGENTIC_PROBE_FILE,
    AGENTIC_TOOL_EDIT,
    AGENTIC_TOOL_READ,
    ProbeExchange,
)
from verdict.screen_record import (
    BACKOFF_MIN_COUNTED_RESULTS,
    BACKOFF_PROBE_INTERVAL,
    RECORD_FILE,
    RECORD_WINDOW,
    THIRD_GROUP_PROBE_SHARE,
    ScreenRecord,
    result_kind,
)
from verdict.subagent_selection import HealthResult

T0 = datetime(2026, 11, 28, 6, 20, tzinfo=timezone.utc)
pytestmark = pytest.mark.usefixtures("no_gateway_network")


# -- scripted transports (no network) ----------------------------------------


def _passing(calls):
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


def _answer_without_tool(route):
    return ProbeExchange(
        http_status=200,
        ok=True,
        reported_model=route,
        latency_ms=2,
        response_body={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
    )


_FIXED = {
    "tool_failure": _answer_without_tool,
    "catalog_stale": lambda route: ProbeExchange(
        http_status=400, ok=False, error_category="catalog_stale"
    ),
    "not_found": lambda route: ProbeExchange(http_status=404, ok=False),
    # What a refusal by the gateway looks like to the probe is not confirmed against
    # the live gateway; a 5xx and a timeout are the two shapes the transport maps.
    "upstream": lambda route: ProbeExchange(http_status=503, ok=False),
    "timeout": lambda route: ProbeExchange(http_status=None, ok=False, error_category="timeout"),
    "rate_limited": lambda route: ProbeExchange(http_status=429, ok=False),
}


def scripted(calls, by_route=None, default="tool_failure"):
    """Each route gets its scripted outcome; ``pass`` runs the real three-turn script."""
    passing = _passing(calls)
    by_route = by_route or {}

    def send(route, payload, timeout):
        outcome = by_route.get(route, default)
        if outcome == "pass":
            return passing(route, payload, timeout)
        calls.append((route, payload))
        return _FIXED[outcome](route)

    return send


# -- catalog ------------------------------------------------------------------


def catalog(specs, admitted=()):
    """Free routes from ``(route_id, owned_by, tools)``; ``tools`` None omits the fact.

    ``admitted`` routes carry healthy traffic, which is what the passive check reads.
    """
    rows, identities = {}, {}
    for route_id, owned_by, tools in specs:
        row = {"id": route_id, "owned_by": owned_by, "root": route_id.split("/", 1)[1]}
        if tools is not None:
            row["capabilities"] = {"tool_calling": tools}
        rows[route_id] = row
        identities[route_id] = (owned_by, row["root"])
    ids = [spec[0] for spec in specs]
    prefixes = frozenset(r.split("/", 1)[0] for r in ids)
    return OmniRouteEvidence(
        fetched_at=T0,
        connections=(),
        usable_providers=prefixes,
        proven_by_traffic={
            r: TrafficEvidence(r, r.split("/", 1)[0], "healthy", 2, 2, T0) for r in admitted
        },
        cooled_models=frozenset(),
        source_errors=(),
        route_identities=identities,
        route_capacities=dict.fromkeys(ids, "free"),
        inventory_route_ids=frozenset(ids),
        inventory_rows=rows,
    )


def make_cache(tmp_path):
    # The default bucket lets a provider send three probes a minute; the clock here is
    # fixed, so give the tests room and leave the pacing to its own tests.
    return HealthCache(tmp_path / "health.json", bucket_capacity=1000)


def run(tmp_path, specs, *, calls=None, transport=None, admitted=(), at=T0, **kwargs):
    calls = [] if calls is None else calls
    options = dict(
        metadata=None,
        cache=make_cache(tmp_path),
        transport=transport or scripted(calls),
        allow_live_probe=True,
        max_per_provider=50,
        max_routes=50,
        clock=lambda: at,
        capacities=("free",),
    )
    options.update(kwargs)
    return identify.screen_models(catalog(specs, admitted), **options)


def probed(calls):
    return list(dict.fromkeys(route for route, _ in calls))


def results(report):
    return {row["route_id"]: row["result"] for row in report["routes"]}


def provider(report, name):
    return report["provider_record"]["providers"][name]


def seed(tmp_path, entries, now=T0):
    """Put results into the record as an earlier run would have: (provider, route, kind, at)."""
    record = ScreenRecord.load(tmp_path / RECORD_FILE)
    for name, route, kind, at in entries:
        record.add(name, route, kind, at)
    record.save(now)


def bad_provider_specs(count=12, owned_by="bad"):
    return [(f"bad/m{i:02d}", owned_by, True) for i in range(count)]


# -- the named thresholds ------------------------------------------------------


def test_thresholds_are_named_constants_with_the_designed_values():
    assert BACKOFF_MIN_COUNTED_RESULTS == 8
    assert timedelta(days=7) == RECORD_WINDOW
    assert timedelta(hours=24) == BACKOFF_PROBE_INTERVAL
    assert THIRD_GROUP_PROBE_SHARE == 8


# -- task 1.2: a route that does not declare tool support is not probed --------


def test_route_without_declared_tool_support_is_not_probed_and_reported(tmp_path):
    """Catches the 2026-11-27 screen, which probed 18 routes whose catalog says no tools."""
    calls = []
    report = run(
        tmp_path,
        calls=calls,
        specs=[
            ("ddgw/no-tools", "duckduckgo-web", False),
            ("ddgw/tools-unstated", "duckduckgo-web", None),
            ("ok/tools", "ok", True),
        ],
    )
    assert probed(calls) == ["ok/tools"]
    assert results(report)["ddgw/no-tools"] == "skipped_no_tools"
    assert results(report)["ddgw/tools-unstated"] == "skipped_no_tools"
    assert report["routes_checked"] == 1


def test_skipped_no_tools_takes_no_cap_alias_or_provider_slot(tmp_path):
    """Catches a no-tools id claiming the model's alias slot or the run's single probe."""
    calls = []
    report = run(
        tmp_path,
        calls=calls,
        max_routes=1,
        max_per_provider=1,
        specs=[
            ("ddgw/same-model", "duckduckgo-web", False),
            ("duckduckgo-web/same-model", "duckduckgo-web", True),
        ],
    )
    # Both ids are one model (same owned_by and root); the id that declares tools is
    # probed, the other is not an alias of it.
    assert probed(calls) == ["duckduckgo-web/same-model"]
    assert results(report) == {
        "ddgw/same-model": "skipped_no_tools",
        "duckduckgo-web/same-model": "fail",
    }
    assert report["routes_checked"] == 1
    assert report["stopped_reason"] == "complete"


def test_route_with_no_catalog_row_is_still_probed(tmp_path):
    """A missing row is not a declaration; only a row that fails to declare tools skips."""
    calls = []
    evidence = catalog([("ok/tools", "ok", True), ("gone/row", "gone", True)])
    evidence = OmniRouteEvidence(
        **{**evidence.__dict__, "inventory_rows": {"ok/tools": evidence.inventory_rows["ok/tools"]}}
    )
    report = identify.screen_models(
        evidence,
        metadata=None,
        cache=make_cache(tmp_path),
        transport=scripted(calls),
        allow_live_probe=True,
        max_per_provider=50,
        max_routes=50,
        clock=lambda: T0,
        capacities=("free",),
    )
    assert set(probed(calls)) == {"ok/tools", "gone/row"}
    assert "skipped_no_tools" not in results(report).values()


def test_rescreen_request_still_probes_a_route_without_declared_tools(tmp_path):
    """Acceptance asks for contested proofs to be proved again; the hint is consumed by a probe.

    This is why the 2026-11-27 00:20 and 06:20 runs probed some ddgw routes: five of
    their six probes carried a rescreen request. The other probes had none, which is
    the default path this change closes.
    """
    cache = make_cache(tmp_path)
    cache.request_rescreen(["ddgw/contested"], now=T0)
    calls = []
    report = run(
        tmp_path,
        calls=calls,
        cache=cache,
        specs=[
            ("ddgw/contested", "duckduckgo-web", False),
            ("ddgw/other", "duckduckgo-web", False),
        ],
    )
    assert probed(calls) == ["ddgw/contested"]
    assert results(report)["ddgw/other"] == "skipped_no_tools"
    assert "ddgw/contested" not in make_cache(tmp_path).rescreen_requests()


# -- task 1.3: a provider that never passes is backed off ----------------------


@pytest.mark.parametrize("kind", ["tool_failure", "catalog_stale", "not_found"])
def test_provider_with_eight_counted_results_and_no_pass_is_backed_off(tmp_path, kind):
    """Catches the 2026-11-27 screen, which sent 27 probes to a provider that passed none."""
    calls = []
    report = run(
        tmp_path, bad_provider_specs(12), calls=calls, transport=scripted(calls, default=kind)
    )
    assert len(probed(calls)) == BACKOFF_MIN_COUNTED_RESULTS
    skipped = [r for r, result in results(report).items() if result == "skipped_backoff"]
    assert len(skipped) == 12 - BACKOFF_MIN_COUNTED_RESULTS
    record = provider(report, "bad")
    assert (record["passes"], record["counted_results"], record["probes"]) == (0, 8, 8)
    assert record["by_kind"] == {kind: 8}
    assert record["backed_off"] is True
    assert record["pass_rate"] == 0.0


def test_seven_counted_results_do_not_back_off(tmp_path):
    calls = []
    report = run(tmp_path, bad_provider_specs(7), calls=calls)
    assert len(probed(calls)) == 7
    assert provider(report, "bad")["backed_off"] is False
    assert "skipped_backoff" not in results(report).values()


@pytest.mark.parametrize("kind", ["upstream", "timeout"])
def test_results_that_are_not_counted_never_back_a_provider_off(tmp_path, kind):
    """Catches a provider the gateway refused or that timed out being treated as a failure."""
    calls = []
    report = run(
        tmp_path, bad_provider_specs(12), calls=calls, transport=scripted(calls, default=kind)
    )
    assert len(probed(calls)) == 12
    record = provider(report, "bad")
    assert record["by_kind"] == {kind: 12}
    assert (record["counted_results"], record["passes"]) == (0, 0)
    assert record["pass_rate"] is None
    assert record["backed_off"] is False


def test_rate_limit_is_recorded_as_its_own_kind_not_the_routes_older_failure(tmp_path):
    """Catches classifying a probe from the cache entry when this probe wrote none.

    A rate-limited probe writes no entry, so the entry is the route's previous
    result; yesterday's catalog_stale must not be counted for today's 429.
    """
    cache = make_cache(tmp_path)
    cache.record(
        "bad/m00",
        ProbeResult(
            category="catalog_stale",
            http_status=400,
            chat_ok=False,
            tool_ok=False,
            probe_class="agentic",
        ),
        T0 - timedelta(days=3),
    )
    cache.save()
    calls = []
    report = run(
        tmp_path,
        [("bad/m00", "bad", True)],
        calls=calls,
        cache=cache,
        transport=scripted(calls, default="rate_limited"),
    )
    assert results(report)["bad/m00"] == "rate_limited"
    record = provider(report, "bad")
    assert record["by_kind"] == {"rate_limited": 1}
    assert record["counted_results"] == 0


def test_a_provider_is_probed_once_a_day_while_backed_off_and_a_pass_ends_it(tmp_path):
    """Back-off and return over a sequence of runs with a fake clock."""
    specs = [*bad_provider_specs(20), ("ok/m", "ok", True)]
    calls = []
    first = run(tmp_path, specs, calls=calls, transport=scripted(calls, {"ok/m": "pass"}))
    assert provider(first, "bad")["backed_off"] is True
    assert provider(first, "ok")["passes"] == 1

    # An hour later nothing of the backed-off provider is probed.
    calls = []
    run(tmp_path, specs, calls=calls, at=T0 + timedelta(hours=1))
    assert not [r for r in probed(calls) if r.startswith("bad/")]

    # Just over a day later exactly one probe goes to it, and the next run, an hour
    # after that, has none.
    calls = []
    third = run(tmp_path, specs, calls=calls, at=T0 + timedelta(hours=25))
    assert len([r for r in probed(calls) if r.startswith("bad/")]) == 1
    assert "skipped_backoff" in results(third).values()
    calls = []
    run(tmp_path, specs, calls=calls, at=T0 + timedelta(hours=26))
    assert not [r for r in probed(calls) if r.startswith("bad/")]

    # A day after that the one probe passes: the back-off ends in that run and the
    # provider's other routes are probed again.
    calls = []
    fifth = run(
        tmp_path,
        specs,
        calls=calls,
        transport=scripted(calls, default="pass"),
        at=T0 + timedelta(hours=50),
    )
    assert len([r for r in probed(calls) if r.startswith("bad/")]) > 1
    assert provider(fifth, "bad")["backed_off"] is False
    assert provider(fifth, "bad")["passes"] >= 1


def test_a_pass_in_the_window_prevents_back_off(tmp_path):
    calls = []
    report = run(
        tmp_path,
        bad_provider_specs(12),
        calls=calls,
        transport=scripted(calls, {"bad/m00": "pass"}),
    )
    assert len(probed(calls)) == 12
    record = provider(report, "bad")
    assert (record["passes"], record["counted_results"]) == (1, 12)
    assert record["backed_off"] is False


def test_results_older_than_seven_days_are_not_counted(tmp_path):
    specs = bad_provider_specs(20)
    calls = []
    run(tmp_path, specs, calls=calls)
    assert len(probed(calls)) == BACKOFF_MIN_COUNTED_RESULTS
    # Just inside the window the provider is still backed off: its one probe for the day.
    calls = []
    inside = run(tmp_path, specs, calls=calls, at=T0 + RECORD_WINDOW - timedelta(minutes=1))
    assert len(probed(calls)) == 1
    assert provider(inside, "bad")["counted_results"] == BACKOFF_MIN_COUNTED_RESULTS + 1
    # Two days on, the first eight have left the window and only that one probe is
    # counted, so seven more are sent before the provider is backed off again.
    calls = []
    outside = run(tmp_path, specs, calls=calls, at=T0 + RECORD_WINDOW + timedelta(days=2))
    assert len(probed(calls)) == BACKOFF_MIN_COUNTED_RESULTS - 1
    assert provider(outside, "bad")["counted_results"] == BACKOFF_MIN_COUNTED_RESULTS


def test_the_record_is_kept_per_catalog_provider_not_per_id_prefix(tmp_path):
    """Catches keying on the route id prefix: two prefixes can be one provider."""
    specs = [(f"alias/m{i:02d}", "realprov", True) for i in range(10)] + [
        ("other/late", "realprov", True)
    ]
    calls = []
    report = run(tmp_path, specs, calls=calls)
    assert set(report["provider_record"]["providers"]) == {"realprov"}
    assert results(report)["other/late"] == "skipped_backoff"
    assert "other/late" not in probed(calls)


# -- task 1.4: order by the provider's record ------------------------------------


def test_order_is_admitted_routes_then_passing_providers_then_the_rest(tmp_path):
    seed(tmp_path, [("good", "good/seed", "pass", T0 - timedelta(days=1))])
    specs = [
        ("adm/a", "adm", True),
        ("adm/b", "adm", True),
        ("bad/b1", "bad", True),
        ("good/x1", "good", True),
        ("good/x2", "good", True),
        ("new/n1", "new", True),
    ]
    calls = []
    run(tmp_path, specs, calls=calls, admitted=("adm/a", "adm/b"))
    assert probed(calls) == ["adm/a", "adm/b", "good/x1", "good/x2", "bad/b1", "new/n1"]


def test_inside_a_group_the_providers_pass_rate_comes_first(tmp_path):
    day = T0 - timedelta(days=1)
    seed(
        tmp_path,
        [("p1", "p1/s", "pass", day)]
        + [("p1", f"p1/s{i}", "tool_failure", day) for i in range(3)]
        + [("p2", f"p2/s{i}", "pass", day) for i in range(3)]
        + [("p2", "p2/s9", "tool_failure", day)],
    )
    specs = [("p1/a", "p1", True), ("p2/a", "p2", True)]
    calls = []
    run(tmp_path, specs, calls=calls)
    # Route-id order would put p1 first; p2 passes three times in four.
    assert probed(calls) == ["p2/a", "p1/a"]


def test_admitted_routes_are_ordered_by_provider_pass_rate_then_existing_order(tmp_path):
    day = T0 - timedelta(days=1)
    seed(
        tmp_path,
        [("m1", "m1/s", "pass", day)]
        + [("m1", f"m1/s{i}", "tool_failure", day) for i in range(3)]
        + [("m2", f"m2/s{i}", "pass", day) for i in range(3)]
        + [("m2", "m2/s9", "tool_failure", day)],
    )
    specs = [("m1/a", "m1", True), ("m2/a", "m2", True), ("m3/a", "m3", True)]
    calls = []
    run(tmp_path, specs, calls=calls, admitted=("m1/a", "m2/a", "m3/a"))
    assert probed(calls) == ["m2/a", "m1/a", "m3/a"]


def _ratings(scores):
    records = tuple(
        ModelMetadataRecord(
            id=route,
            omniroute_ids=(route,),
            scores=SoftScores(
                aa_coding=ProvenancedField(
                    value=score,
                    provenance=FieldProvenance(source="openrouter_aa", fetched_at=T0.isoformat()),
                )
            ),
        )
        for route, score in scores.items()
    )
    return MetadataSnapshot(
        schema_version="1", refreshed_at=T0.isoformat(), sources={}, records=records
    )


def test_the_rating_breaks_ties_after_the_providers_pass_rate(tmp_path):
    """Inside a group: pass rate first, then Verdict's rating (the order before this change)."""
    metadata = _ratings({"a/weak": 10, "z/strong": 90})
    specs = [("a/weak", "a", True), ("z/strong", "z", True)]
    both = ("a/weak", "z/strong")
    day = T0 - timedelta(days=1)

    # No record: the rating alone decides, strongest first (route-id order would not).
    calls = []
    run(tmp_path / "none", specs, calls=calls, admitted=both, metadata=metadata)
    assert probed(calls) == ["z/strong", "a/weak"]

    # Equal pass rates: still the rating.
    (tmp_path / "equal").mkdir()
    seed(tmp_path / "equal", [("a", "a/s", "pass", day), ("z", "z/s", "pass", day)])
    calls = []
    run(tmp_path / "equal", specs, calls=calls, admitted=both, metadata=metadata)
    assert probed(calls) == ["z/strong", "a/weak"]

    # A better pass rate beats a better rating.
    (tmp_path / "rate").mkdir()
    seed(
        tmp_path / "rate",
        [("a", "a/s", "pass", day), ("z", "z/s", "pass", day), ("z", "z/t", "tool_failure", day)],
    )
    calls = []
    run(tmp_path / "rate", specs, calls=calls, admitted=both, metadata=metadata)
    assert probed(calls) == ["a/weak", "z/strong"]


def test_one_probe_in_eight_goes_to_the_third_group(tmp_path):
    """Catches the second group crowding out a provider that has no record yet."""
    admitted = [f"adm/r{i:02d}" for i in range(20)]
    specs = [(r, "adm", True) for r in admitted] + [(f"zz/n{i}", "zz", True) for i in range(3)]
    calls = []
    run(
        tmp_path,
        specs,
        calls=calls,
        admitted=admitted,
        max_routes=17,
        transport=scripted(calls, default="pass"),
    )
    order = probed(calls)
    assert len(order) == 17
    third = [i for i, route in enumerate(order) if route.startswith("zz/")]
    assert third == [7, 15]
    assert order[:7] == admitted[:7]


def test_the_third_group_share_counts_probes_not_skipped_candidates(tmp_path):
    """A third-group candidate that is skipped is not a probe: the next one is taken."""
    cache = make_cache(tmp_path)
    cache.record(
        "zz/n0",
        ProbeResult(
            category="catalog_stale",
            http_status=400,
            chat_ok=False,
            tool_ok=False,
            probe_class="agentic",
        ),
        T0,
    )
    cache.save()
    admitted = [f"adm/r{i:02d}" for i in range(12)]
    specs = [(r, "adm", True) for r in admitted] + [(f"zz/n{i}", "zz", True) for i in range(3)]
    calls = []
    report = run(
        tmp_path,
        specs,
        calls=calls,
        cache=cache,
        admitted=admitted,
        max_routes=9,
        transport=scripted(calls, default="pass"),
    )
    assert results(report)["zz/n0"] == "skipped_negative"
    assert probed(calls)[7] == "zz/n1"
    assert report["routes_checked"] == 9


def test_a_provider_with_no_record_still_receives_a_probe_while_others_have_many(tmp_path):
    seed(tmp_path, [("good", "good/seed", "pass", T0 - timedelta(hours=3))])
    specs = [(f"good/m{i:02d}", "good", True) for i in range(30)] + [("new/first", "new", True)]
    calls = []
    run(tmp_path, specs, calls=calls, max_routes=8)
    assert "new/first" in probed(calls)


def test_rescreen_request_no_longer_moves_a_route_ahead(tmp_path):
    """Catches the old sort that put every rescreen-requested route first."""
    cache = make_cache(tmp_path)
    cache.request_rescreen(["adm/b"], now=T0)
    specs = [("adm/a", "adm", True), ("adm/b", "adm", True)]
    calls = []
    run(tmp_path, specs, calls=calls, cache=cache, admitted=("adm/a", "adm/b"), max_routes=1)
    assert probed(calls) == ["adm/a"]


# -- the report ----------------------------------------------------------------


def test_report_carries_results_by_kind_for_each_provider_and_whether_it_is_backed_off(tmp_path):
    specs = [*bad_provider_specs(12), ("ok/a", "ok", True), ("ok/b", "ok", True)]
    calls = []
    report = run(
        tmp_path,
        specs,
        calls=calls,
        transport=scripted(calls, {"ok/a": "pass", "ok/b": "upstream"}),
    )
    block = report["provider_record"]
    assert block["window_days"] == 7
    assert block["error"] is None
    assert provider(report, "bad") == {
        "passes": 0,
        "counted_results": 8,
        "probes": 8,
        "by_kind": {"tool_failure": 8},
        "pass_rate": 0.0,
        "backed_off": True,
        "last_probe_at": T0.isoformat(),
    }
    ok = provider(report, "ok")
    assert ok["by_kind"] == {"pass": 1, "upstream": 1}
    assert (ok["passes"], ok["counted_results"], ok["probes"]) == (1, 1, 2)
    assert ok["pass_rate"] == 1.0 and ok["backed_off"] is False
    json.dumps(report, allow_nan=False)


def test_the_report_names_the_record_when_the_screen_could_not_take_the_lease(tmp_path):
    seed(tmp_path, [("bad", "bad/s", "tool_failure", T0 - timedelta(hours=1))])
    lock = _JobLock(tmp_path / "verified-refresh.lock")
    assert lock.acquire()
    try:
        calls = []
        report = run(tmp_path, bad_provider_specs(3), calls=calls)
    finally:
        lock.release()
    assert calls == []
    assert report["stopped_reason"] == "lock_timeout"
    assert provider(report, "bad")["probes"] == 1
    # Read only: nothing was added.
    assert ScreenRecord.load(tmp_path / RECORD_FILE).summary("bad", T0).probes == 1


def test_an_unreadable_record_leaves_the_screen_probing_and_says_so(tmp_path):
    """Nothing remembered means nothing backed off: the screen probes as it did before."""
    day = T0 - timedelta(hours=3)
    seed(tmp_path, [("bad", f"bad/s{i}", "tool_failure", day) for i in range(9)])
    (tmp_path / RECORD_FILE).write_text("{")
    calls = []
    report = run(tmp_path, bad_provider_specs(3), calls=calls)
    assert len(probed(calls)) == 3
    assert report["provider_record"]["error"] == "unreadable"
    # The next save replaced the file with a readable one.
    assert ScreenRecord.load(tmp_path / RECORD_FILE).summary("bad", T0).probes == 3


def test_a_record_that_cannot_be_written_does_not_stop_the_screen(tmp_path):
    (tmp_path / RECORD_FILE).mkdir()  # not a file: it cannot be read or replaced
    calls = []
    report = run(tmp_path, bad_provider_specs(3), calls=calls)
    assert len(probed(calls)) == 3
    assert report["provider_record"]["error"] == "unwritable"


# -- no signal admits a route ---------------------------------------------------


def _ladder(tmp_path, cache, route_id, owned_by):
    rows = [
        {
            "id": route_id,
            "owned_by": owned_by,
            "context_length": 200_000,
            "max_input_tokens": 200_000,
            "max_output_tokens": 32_000,
            "capabilities": {"tool_calling": True, "reasoning": True},
            "pricing": {"input": 0, "output": 0},
        }
    ]
    connections = [
        {
            "provider": owned_by,
            "authType": "apikey",
            "isActive": True,
            "testStatus": "ok",
            "backoffLevel": 0,
            "plan_label": "free",
            "import_free_only": True,
        }
    ]
    ladder = EligibilityLadder(
        rows,
        connections,
        lambda route: HealthResult(healthy=True, category=""),
        tmp_path / "state.json",
        health_cache=cache,
    )
    return {v.route_id: v for v in ladder.evaluate(TaskRequirements(), now=T0)}[route_id]


def test_every_signal_favourable_and_no_proof_is_still_no_agentic_probe(tmp_path):
    """The record, the order and the passive check are scheduling hints, never proof.

    ``free/model-a`` is passively admitted, declares tools and belongs to a provider
    with passes; another provider has a better rate, so it takes the run's one probe
    and ``free/model-a`` is not probed. The ladder still refuses it. A real pass
    afterwards is the control: the same ladder then admits it.
    """
    day = T0 - timedelta(hours=2)
    seed(
        tmp_path,
        [("free", f"free/s{i}", "pass", day) for i in range(3)]
        + [("free", "free/s9", "tool_failure", day)]
        + [("best", f"best/s{i}", "pass", day) for i in range(4)],
    )
    specs = [("free/model-a", "free", True), ("best/model-b", "best", True)]
    cache = make_cache(tmp_path)
    calls = []
    report = run(
        tmp_path,
        specs,
        calls=calls,
        cache=cache,
        transport=scripted(calls, default="pass"),
        admitted=("free/model-a", "best/model-b"),
        max_routes=1,
    )
    assert probed(calls) == ["best/model-b"]
    assert results(report)["free/model-a"] == "skipped_cap"
    cache.reload()
    assert cache.entry("free/model-a") is None
    verdict = _ladder(tmp_path, cache, "free/model-a", "free")
    assert verdict.rank is None
    assert verdict.reason == "no_agentic_probe"

    # Control: a real pass of that route makes the same ladder admit it.
    calls = []
    run(
        tmp_path,
        specs,
        calls=calls,
        cache=cache,
        transport=scripted(calls, default="pass"),
        admitted=("free/model-a", "best/model-b"),
        max_routes=2,
    )
    assert "free/model-a" in probed(calls)
    cache.reload()
    assert _ladder(tmp_path, cache, "free/model-a", "free").rank is not None


# -- the record file -------------------------------------------------------------


def test_result_kind_reads_the_entry_only_for_a_failure():
    assert result_kind("pass", "agentic_fail") == "pass"
    assert result_kind("fail", "agentic_fail") == "tool_failure"
    assert result_kind("fail", "ok") == "tool_failure"
    assert result_kind("fail", "catalog_stale") == "catalog_stale"
    assert result_kind("fail", "not_found") == "not_found"
    assert result_kind("fail", "http_error") == "http_error"
    assert result_kind("fail", None) == "fail"
    # A result that wrote no entry keeps its own name whatever the old entry says.
    for result in ("rate_limited", "auth_failed", "identity_mismatch", "skipped_deadline"):
        assert result_kind(result, "catalog_stale") == result


def test_the_record_file_drops_invalid_entries_and_says_so(tmp_path):
    path = tmp_path / RECORD_FILE
    good = {"provider": "p", "route_id": "p/a", "kind": "pass", "at": T0.isoformat()}
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "results": [
                    good,
                    {**good, "at": "2026-11-28T06:20:00"},  # naive timestamp
                    {**good, "kind": ""},
                    {**good, "at": "not a time"},
                    "text",
                ],
            }
        )
    )
    record = ScreenRecord.load(path)
    assert record.error == "invalid_entries_dropped"
    assert record.summary("p", T0).probes == 1


@pytest.mark.parametrize(
    "text", ["{", "[]", '{"schema_version": 2, "results": []}', '{"results": 1}']
)
def test_a_malformed_record_file_is_an_empty_record_with_an_error(tmp_path, text):
    path = tmp_path / RECORD_FILE
    path.write_text(text)
    record = ScreenRecord.load(path)
    assert record.error == "unreadable"
    assert record.summaries(T0) == []


def test_a_missing_record_file_is_an_empty_record_without_an_error(tmp_path):
    record = ScreenRecord.load(tmp_path / RECORD_FILE)
    assert record.error is None and record.summaries(T0) == []


def test_saving_drops_what_left_the_window_and_round_trips(tmp_path):
    record = ScreenRecord.load(tmp_path / RECORD_FILE)
    record.add("p", "p/old", "pass", T0 - RECORD_WINDOW - timedelta(minutes=1))
    record.add("p", "p/new", "tool_failure", T0 - timedelta(hours=1))
    record.save(T0)
    assert record.error is None
    again = ScreenRecord.load(tmp_path / RECORD_FILE)
    summary = again.summary("p", T0)
    assert (summary.probes, summary.passes, summary.counted) == (1, 0, 1)
    mode = (tmp_path / RECORD_FILE).stat().st_mode & 0o777
    assert mode == 0o600
