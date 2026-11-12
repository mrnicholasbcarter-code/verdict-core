"""BOD-297 red-first audit tests: every qualification producer, one coordinator.

Audit: docs/quality/BOD-297-QUALIFICATION-PRODUCER-AUDIT.md. Each ``red`` test is
a strict xfail that must fail with an AssertionError on the audited code: it
documents one gap. When a follow-up card closes the gap the test XPASSes, the
strict marker fails the run, and the card removes the marker. Guard tests
(no marker) pin behaviour that already holds and must keep holding.

No test sends a real request: every transport is an in-process fake, state
lives under ``tmp_path`` and the autouse fixtures isolate HOME.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

from verdict import identify
from verdict.admit_prove_confirm import gate_admit_prove_confirm
from verdict.availability import ModelInfo
from verdict.free_tier_admit import admit_free_tier_active, snapshot_from_payloads
from verdict.identify_pools import build_task_pools
from verdict.model_passports import run_qualification
from verdict.omniroute_evidence import (
    ConnectionHealth,
    LiveCandidate,
    OmniRouteEvidence,
    TrafficEvidence,
)
from verdict.orchestration.health_cache import HealthCache, HealthEntry, ProbeResult, ScopedCooldown
from verdict.orchestration.verified_refresh import (
    RefreshConfig,
    RefreshCoordinator,
    RefreshSnapshot,
    RowInput,
    _JobLock,
)
from verdict.prove_at_rest import (
    AGENTIC_PROBE_FILE,
    AGENTIC_TOOL_EDIT,
    AGENTIC_TOOL_READ,
    AGENTIC_TURNS,
    AdmittedRoute,
    ProbeExchange,
    Prober,
)

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "omniroute_evidence" / "snapshot.json"
pytestmark = pytest.mark.usefixtures("no_gateway_network")


def red(reason: str) -> pytest.MarkDecorator:
    """A documented BOD-297 gap: must fail on an assertion until fixed."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"BOD-297 gap: {reason}")


# -- shared fakes ------------------------------------------------------------


def chat_ok(calls: list[tuple[str, str]] | None = None, *, echo: bool = True) -> Any:
    """Chat+tool transport that passes; ``echo`` False omits the served model id."""

    def send(route: str, phase: str, timeout: float) -> ProbeExchange:
        if calls is not None:
            calls.append((route, phase))
        model = route.split("/", 1)[1] if echo else ""
        return ProbeExchange(
            http_status=200, ok=True, chat_exact=True, tool_called=True, reported_model=model
        )

    return send


_SCRIPT = [
    (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
    (
        AGENTIC_TOOL_EDIT,
        {"path": AGENTIC_PROBE_FILE, "old_text": "line two", "new_text": "LINE TWO"},
    ),
    (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
]


def agentic_ok(calls: list[str], hook: Any = None) -> Any:
    """Three-turn read/edit/read transport that passes; ``hook(route, turn)`` runs first."""

    def send(route: str, payload: dict[str, Any], timeout: float) -> ProbeExchange:
        turn = sum(m.get("role") == "user" for m in payload["messages"]) - 1
        calls.append(route)
        if hook is not None:
            hook(route, turn)
        name, args = _SCRIPT[turn]
        call = {"id": f"t{turn}", "type": "function"}
        call["function"] = {"name": name, "arguments": json.dumps(args)}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return ProbeExchange(
            http_status=200,
            ok=True,
            reported_model=route,
            response_body={"model": route, "choices": [{"message": message}]},
        )

    return send


def coordinator(tmp: Path, transport: Any, clock: Any = lambda: NOW) -> RefreshCoordinator:
    return RefreshCoordinator(
        cache=HealthCache(tmp / "health.json"),
        transport=transport,
        clock=clock,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp / "verified-refresh.lock",
        marker_path=tmp / "verified-refresh.json",
    )


def free_row(route: str, **kwargs: Any) -> RowInput:
    return RowInput(route, route.split("/", 1)[0], "UNVERIFIED", "free", refreshable=True, **kwargs)


def refresh(c: RefreshCoordinator, rows: tuple[RowInput, ...], **config: Any) -> Any:
    return c.refresh_for_consumer(
        RefreshSnapshot(rows=rows, scope="bod297"),
        consumer="census",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(**config),
        explicit=True,
    )


def evidence(
    routes: dict[str, str], *, traffic: tuple[str, ...] | None = None, live: tuple[str, ...] = ()
) -> OmniRouteEvidence:
    """Passive evidence: ``routes`` maps route -> capacity; providers are id prefixes."""
    seen = tuple(routes) if traffic is None else traffic
    provider = {r: r.split("/", 1)[0] for r in routes}
    return OmniRouteEvidence(
        fetched_at=NOW,
        connections=(),
        usable_providers=frozenset(provider.values()),
        proven_by_traffic={r: TrafficEvidence(r, provider[r], "healthy", 2, 2, NOW) for r in seen},
        cooled_models=frozenset(),
        source_errors=(),
        route_identities={r: (provider[r], r.split("/", 1)[1]) for r in routes},
        route_capacities=dict(routes),
        inventory_route_ids=frozenset(routes),
        live_candidates={r: LiveCandidate(r, provider[r], ("c1",), True) for r in live},
    )


def screen(cache_path: Path, ev: OmniRouteEvidence, transport: Any, **kwargs: Any) -> Any:
    options: dict[str, Any] = dict(
        metadata=None,
        cache=HealthCache(cache_path),
        transport=transport,
        allow_live_probe=True,
        max_per_provider=1,
        max_routes=1,
        clock=lambda: NOW,
    )
    options.update(kwargs)
    return identify.screen_models(ev, **options)


def confirm_snapshot() -> Any:
    return snapshot_from_payloads(
        catalog={"data": [{"id": "opencode/hy3-free", "owned_by": "opencode"}]},
        free_tier={
            "perModel": [{"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"}]
        },
        providers={
            "connections": [{"provider": "opencode", "isActive": True, "testStatus": "active"}]
        },
    )


def confirm_transport(calls: list[str]) -> Any:
    def send(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        calls.append(model_id)
        body = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return {"status_code": 200, "body": body}

    return send


def quota_snapshot(account_status: object) -> Any:
    return snapshot_from_payloads(
        catalog={"data": [{"id": "good/a", "owned_by": "good"}]},
        free_tier={"perModel": [{"modelId": "a", "provider": "good", "freeType": "keyless"}]},
        providers={"connections": [{"provider": "good", "isActive": True, "testStatus": "active"}]},
        account_status=account_status,
    )


# -- (a) every producer joins the one coordinator lease ----------------------


@red("identify --screen dispatches outside the RefreshCoordinator flock/marker")
def test_identify_screen_joins_the_shared_coordinator_lease(tmp_path: Path) -> None:
    """Another producer owns the coordinator job: identify must join it, not send."""
    calls: list[str] = []
    held = _JobLock(tmp_path / "verified-refresh.lock")
    assert held.acquire()
    try:
        screen(tmp_path / "health.json", evidence({"p/a": "free"}), agentic_ok(calls))
    finally:
        held.release()
    assert calls == [], "identify screen sent model requests while another producer held the lease"


@red("live admit confirm dispatches outside the RefreshCoordinator flock/marker")
def test_live_confirm_joins_the_shared_coordinator_lease(tmp_path: Path) -> None:
    calls: list[str] = []
    held = _JobLock(tmp_path / "verified-refresh.lock")
    assert held.acquire()
    try:
        gate_admit_prove_confirm(
            admit_free_tier_active(confirm_snapshot()),
            health_cache_path=tmp_path / "health.json",
            passport_store_path=tmp_path / "passports.json",
            confirm_transport=confirm_transport(calls),
            now=NOW,
            live=True,
            consented=True,
        )
    finally:
        held.release()
    assert calls == [], "live confirm sent a request while another producer held the lease"


@red("EligibilityLadder.select confirmation probes outside the coordinator lease")
def test_selection_confirm_probe_joins_the_shared_coordinator_lease(tmp_path: Path) -> None:
    from test_orch_eligibility import NOW as LADDER_NOW
    from test_orch_eligibility import REQ, conn, make_ladder, row

    held = _JobLock(tmp_path / "verified-refresh.lock")
    assert held.acquire()
    try:
        ladder, probe = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude")],
            [conn("claude")],
            health_cache=HealthCache(tmp_path / "health.json"),
        )
        ladder.select(REQ, now=LADDER_NOW)
    finally:
        held.release()
    assert probe.calls == [], "selection probed a route while another producer held the lease"


@red("qualification attempts have no durable RESERVED/MAYBE_SENT/SETTLED record")
def test_coordinator_records_durable_attempt_states(tmp_path: Path) -> None:
    """While a probe is on the wire its attempt is durably MAYBE_SENT; then SETTLED."""
    durable: list[str] = []

    def on_disk() -> str:
        names = ("health.json", "verified-refresh.json")
        return "".join(
            (tmp_path / n).read_text("utf-8") for n in names if (tmp_path / n).exists()
        ).lower()

    def send(route: str, phase: str, timeout: float) -> ProbeExchange:
        durable.append(on_disk())
        return chat_ok()(route, phase, timeout)

    refresh(coordinator(tmp_path, send), (free_row("p/m"),))
    assert durable and all("maybe_sent" in text for text in durable), (
        "no durable MAYBE_SENT attempt existed while the request was in flight"
    )
    assert "settled" in on_disk(), "the finished attempt was never durably SETTLED"


@red("the coordinator has no durable cursor; no-write outcomes starve the backlog")
def test_coordinator_cursor_screens_backlog_within_n_cycles(tmp_path: Path) -> None:
    """Six routes, two per job, no evidence written: three jobs must reach all six."""
    attempted: set[str] = set()

    def busy(route: str, phase: str, timeout: float) -> ProbeExchange:
        attempted.add(route)
        return ProbeExchange(http_status=503, ok=False, error_category="gateway_busy")

    rows = tuple(free_row(f"p{i}/m", rank_hint=i) for i in range(6))
    clock = [NOW]
    for cycle in range(3):
        clock[0] = NOW + timedelta(minutes=10 * cycle)
        c = coordinator(tmp_path, busy, clock=lambda: clock[0])
        refresh(c, rows, max_routes=2, debounce_seconds=0)
    assert attempted == {r.route_id for r in rows}, f"backlog starved; only {sorted(attempted)}"


# -- (b) no caller multiplies limits ------------------------------------------


@red("prove-at-rest adds a separate agentic request budget on top of max_requests")
def test_prove_at_rest_agentic_phase_shares_the_request_budget(tmp_path: Path) -> None:
    chat_calls: list[tuple[str, str]] = []
    agentic_calls: list[str] = []
    routes = [AdmittedRoute(f"p{i}/m", f"p{i}", "free") for i in range(20)]
    Prober(
        HealthCache(tmp_path / "health.json"),
        lambda: routes,
        chat_ok(chat_calls),
        max_requests=16,
        agentic_transport=agentic_ok(agentic_calls),
        clock=lambda: NOW,
    ).run_once()
    total = len(chat_calls) + len(agentic_calls)
    assert total <= 16, f"one cycle with max_requests=16 sent {total} model requests"


@red("the agentic phase consumes bucket tokens in memory, not as a durable reservation")
def test_agentic_phase_persists_its_bucket_reservation_before_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    after_chat: list[int] = []
    at_agentic: list[int] = []

    def chat(route: str, phase: str, timeout: float) -> ProbeExchange:
        after_chat.append(HealthCache(path).bucket_remaining("p", NOW))
        return chat_ok()(route, phase, timeout)

    def record(route: str, turn: int) -> None:
        if turn == 0:
            at_agentic.append(HealthCache(path).bucket_remaining("p", NOW))

    Prober(
        HealthCache(path),
        lambda: [AdmittedRoute("p/m", "p", "free")],
        chat,
        agentic_transport=agentic_ok([], record),
        clock=lambda: NOW,
    ).run_once()
    before = min(after_chat) if after_chat else HealthCache(path).bucket_remaining("p", NOW)
    assert at_agentic, "the agentic phase did not run"
    assert at_agentic[0] <= before - AGENTIC_TURNS, (
        "another process reading the shared cache still sees the agentic tokens as free"
    )


# -- (c) sparse quota unknown is not exhaustion; whole-source failure is closed --


@red("a successful sparse /api/v1/me/status row is read as quota_exhausted")
@pytest.mark.parametrize("reason", ["not_available", "fetch_failed", "connection_lookup_failed"])
def test_sparse_account_quota_row_is_unknown_not_exhausted(reason: str) -> None:
    """One account without quota data in a successful response is unknown, not exhausted."""
    row = {"provider": "good", "connectionId": "c1", "shared": True}
    receipt = admit_free_tier_active(
        quota_snapshot({"accountQuotas": [{**row, "available": False, "reason": reason}]})
    )
    assert receipt.admitted == ("good/a",), (
        f"reason={reason} excluded the route: {[(d.model_id, d.reason) for d in receipt.exclusions]}"
    )


@red("a malformed whole account-status source still admits as if quota were known")
def test_whole_account_status_source_failure_fails_closed() -> None:
    receipt = admit_free_tier_active(quota_snapshot({"error": "upstream failure"}))
    assert receipt.admitted == (), "a failed quota source must fail closed, not admit"


def test_reported_quota_exhaustion_still_blocks() -> None:
    """Guard: fresh explicit exhaustion of the only account keeps blocking."""
    window = {"usedPercentage": 100, "resetAt": "2099-01-01T00:00:00Z"}
    row = {"provider": "good", "connectionId": "c1", "quotas": {"session": window}}
    receipt = admit_free_tier_active(quota_snapshot({"accountQuotas": [row]}))
    assert receipt.admitted == ()
    assert any(d.reason == "quota_exhausted" for d in receipt.exclusions)


# -- (d) all configured supply is considered -----------------------------------


@red("identify --screen considers only routes with historical traffic")
def test_identify_screen_considers_configured_supply_without_traffic(tmp_path: Path) -> None:
    ev = evidence({"p/a": "free", "q/new": "free"}, traffic=("p/a",), live=("q/new",))
    report = screen(tmp_path / "health.json", ev, agentic_ok([]), max_per_provider=2, max_routes=4)
    considered = {r["route_id"] for r in report["routes"]}
    assert "q/new" in considered, "a reachable configured route was never considered"


# -- (e) alias/effort mapping never mints independent or stronger proof ---------


@red("the coordinator probes alias ids of one backend model as independent proofs")
def test_coordinator_probes_one_alias_family_member(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    rows = (free_row("agy/gemini-x"), free_row("antigravity/gemini-x"))
    refresh(coordinator(tmp_path, chat_ok(calls)), rows)
    probed = {route for route, _phase in calls}
    assert len(probed) == 1, f"one backend model was probed as {sorted(probed)}"


@red("a no-think variant's tool proof qualifies the thinking default route")
def test_effort_variant_proof_does_not_qualify_a_different_effort() -> None:
    weaker, default = "no-think/cc/claude-x", "cc/claude-x"
    ev = OmniRouteEvidence(
        fetched_at=NOW,
        connections=(ConnectionHealth("c1", "claude", True, None, NOW, "active"),),
        usable_providers=frozenset({"claude"}),
        proven_by_traffic={},
        cooled_models=frozenset(),
        source_errors=(),
        inventory_route_ids=frozenset({weaker, default}),
        live_candidates={r: LiveCandidate(r, "claude", ("c1",), True) for r in (weaker, default)},
    )
    proof = HealthEntry(
        route_id=weaker,
        category="ok",
        checked_at=NOW,
        until=NOW + timedelta(hours=6),
        consecutive_failures=0,
        chat_ok=True,
        tool_ok=True,
        healthy=True,
        identity="verified",
        agentic_ok=True,
        agentic_checked_at=NOW,
        probe_class="agentic",
    )
    pool = build_task_pools(ev, None, None, {weaker: proof}, NOW)["implementation"]
    eligible = {r["route_id"]: r.get("tool_proof_route") for r in pool["eligible"]}
    assert default not in eligible, f"{default} became eligible on {eligible.get(default)}'s proof"
    assert eligible.get(weaker) == weaker, (
        "the proof must stay credited to the route that earned it"
    )


# -- (f) only a strict served-identity tool receipt mints tool strength --------


@red("a probe with no served-model echo still mints a verified tool proof")
def test_coordinator_requires_served_model_identity_for_tool_strength(tmp_path: Path) -> None:
    outcome = refresh(coordinator(tmp_path, chat_ok(echo=False)), (free_row("p/m"),))
    entry = HealthCache(tmp_path / "health.json").entry("p/m")
    assert outcome.verified == 0, "an unidentified reply counted as a verified route"
    assert entry is None or not entry.tool_ok, "tool strength was minted without a served identity"


@red("run_qualification copies catalog tool capability into passport tool_support")
def test_passport_tool_support_requires_an_observed_tool_call() -> None:
    def chat_only(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        body = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return {"status_code": 200, "body": body}

    passport = run_qualification(
        provider="p",
        model_id="m",
        transport=chat_only,
        model=ModelInfo(id="p/m", provider="p", capabilities=("tools",)),
        require_tools=True,
    )
    assert passport.tool_support is False, "catalog metadata minted tool_support with no tool call"


@red("a late coordinator success overwrites a newer negative and restamps observed time")
def test_late_success_keeps_newer_negative_and_observed_time(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    clock = [NOW]

    def late(route: str, phase: str, timeout: float) -> ProbeExchange:
        if phase == "tool":
            other = HealthCache(path)
            negative = ProbeResult(category="upstream", chat_ok=False, tool_ok=False)
            other.record(route, negative, NOW + timedelta(seconds=5))
            other.save()
            clock[0] = NOW + timedelta(seconds=30)
        return chat_ok()(route, phase, timeout)

    refresh(coordinator(tmp_path, late, clock=lambda: clock[0]), (free_row("p/m"),))
    entry = HealthCache(path).entry("p/m")
    assert entry is not None and not entry.healthy, "an older success replaced a newer negative"


# -- (g) consent, per-run budget and admission before each request; order ------


@red("identify --screen ignores an active scoped provider cooldown")
def test_identify_screen_honours_scoped_provider_cooldown(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    cache.record_cooldown(
        ScopedCooldown(
            key="provider:p",
            category="rate_limited",
            checked_at=NOW,
            until=NOW + timedelta(minutes=10),
            provider_id="p",
        )
    )
    cache.save()
    calls: list[str] = []
    screen(path, evidence({"p/a": "free"}), agentic_ok(calls))
    assert calls == [], "a request was sent into a provider that is cooling down"


@red("identify --screen admits once per conversation, not before each request")
def test_identify_screen_admits_before_each_request(tmp_path: Path) -> None:
    path = tmp_path / "health.json"

    def cool_after_first(route: str, turn: int) -> None:
        if turn == 0:
            other = HealthCache(path)
            other.record_cooldown(
                ScopedCooldown(
                    key="provider:p",
                    category="rate_limited",
                    checked_at=NOW,
                    until=NOW + timedelta(minutes=10),
                    provider_id="p",
                )
            )
            other.save()

    calls: list[str] = []
    screen(path, evidence({"p/a": "free"}), agentic_ok(calls, cool_after_first))
    assert len(calls) == 1, f"{len(calls)} requests were sent after the pool cooled"


@red("live confirm ignores the shared per-provider bucket before its request")
def test_live_confirm_checks_the_shared_pool_bucket(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    cache.zero_bucket("opencode", NOW + timedelta(minutes=10))
    cache.save()
    calls: list[str] = []
    gate_admit_prove_confirm(
        admit_free_tier_active(confirm_snapshot()),
        health_cache_path=tmp_path / "health.json",
        passport_store_path=tmp_path / "passports.json",
        confirm_transport=confirm_transport(calls),
        now=NOW,
        live=True,
        consented=True,
    )
    assert calls == [], "a confirm request was sent while the shared provider bucket was empty"


@red("identify --screen orders by capacity class before task rating")
def test_identify_screen_orders_strongest_task_rating_first(tmp_path: Path) -> None:
    from test_identify_screen import passing, ratings, snapshot

    calls: list[tuple[str, Any]] = []
    report = identify.screen_models(
        snapshot(),
        metadata=ratings(),
        cache=HealthCache(tmp_path / "health.json"),
        transport=passing(calls),
        allow_live_probe=True,
        max_per_provider=1,
        max_routes=2,
        clock=lambda: NOW,
    )
    checked = [r["route_id"] for r in report["routes"] if r["turns_sent"]]
    assert checked == ["p/strong", "q/model"], f"screen order was {checked}"


@red("verdict census sends live model requests without an explicit consent flag")
def test_census_command_requires_explicit_live_probe_consent(monkeypatch: Any) -> None:
    from verdict import census_passports, free_tier_admit
    from verdict.omniroute_pipeline import census_hook

    class ReachedLivePathError(Exception):
        pass

    def live_read(*_args: Any, **_kwargs: Any) -> Any:
        raise ReachedLivePathError

    monkeypatch.setattr(
        free_tier_admit, "omniroute_endpoint_from_env", lambda: ("http://127.0.0.1:20128", None)
    )
    monkeypatch.setattr(free_tier_admit, "load_omniroute_admit_snapshot", live_read)
    monkeypatch.setattr(census_hook, "census_probe_key", lambda: "fixture-key")
    reached = False
    try:
        census_passports.cmd_census()
    except ReachedLivePathError:
        reached = True
    except (ValueError, SystemExit):
        pass
    assert not reached, "census reached the live probing path with no consent flag"


# -- (h) no five-minute model reprobe loop; fresh repeats send nothing ---------


@red("the epsilon slice re-probes an active negative every five-minute cycle")
def test_maintenance_never_reprobes_an_active_negative(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    not_found = ProbeResult(
        category="not_found", chat_ok=False, tool_ok=False, http_status=404, failure_scope="route"
    )
    cache.record("p/m", not_found, NOW - timedelta(minutes=1))
    cache.save()
    calls: list[tuple[str, str]] = []

    def still_missing(route: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append((route, phase))
        return ProbeExchange(http_status=404, ok=False)

    clock = [NOW]
    prober = Prober(
        HealthCache(tmp_path / "health.json"),
        lambda: [AdmittedRoute("p/m", "p", "free")],
        still_missing,
        clock=lambda: clock[0],
    )
    for cycle in range(4):
        clock[0] = NOW + timedelta(seconds=300 * cycle)
        prober.run_once()
    assert calls == [], f"an active 404 negative was re-probed {len(calls)} times in 15 minutes"


@red("the coordinator re-probes a route another producer proved after the snapshot")
def test_fresh_repeated_run_rechecks_shared_cache_and_sends_nothing(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    proven = ProbeResult(category="ok", chat_ok=True, tool_ok=True, identity="verified")
    cache.record("p/m", proven, NOW - timedelta(seconds=30))
    cache.save()
    calls: list[tuple[str, str]] = []
    refresh(coordinator(tmp_path, chat_ok(calls)), (free_row("p/m"),))
    assert calls == [], "a route with fresh shared proof was probed again"


# -- (i) passive A3.1 stays 0 POST / 0 model calls / 0 writes ----------------


def test_passive_identify_is_get_only_with_no_model_calls_or_writes(
    monkeypatch: Any, capsys: Any
) -> None:
    """Guard: ``verdict identify --passive`` is GET-only and leaves HOME byte-identical."""
    from verdict import omniroute_evidence

    payloads = json.loads(FIXTURES.read_text("utf-8"))
    sources = {
        "/api/providers": "providers",
        "/api/resilience/connections": "resilience",
        "/api/usage/provider-limits": "limits",
        "/api/resilience/model-cooldowns": "cooldowns",
        "/api/providers/health-matrix": "health_matrix",
        "/v1/models": "v1_models",
        "/v1/auto-combo/auto/candidates": "candidates",
    }
    requests: list[tuple[str, str, bool]] = []

    class Reply(io.BytesIO):
        status = 200
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self) -> Reply:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def get(request: Any, **_kwargs: Any) -> Reply:
        path = urlsplit(request.full_url).path
        requests.append((request.get_method(), path, request.data is not None))
        return Reply(json.dumps(payloads[sources[path]]).encode())

    def tree() -> dict[str, tuple[int, int]]:
        home = Path.home()
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in home.rglob("*")}

    monkeypatch.setenv("OMNIROUTE_API_KEY", "fixture-key")
    monkeypatch.setattr(omniroute_evidence, "open_no_redirect", get)
    before = tree()
    identify.cmd_identify(output_json=True)
    report = json.loads(capsys.readouterr().out)
    assert requests and {method for method, _path, _body in requests} == {"GET"}
    assert not any(body for _method, _path, body in requests)
    assert report["model_calls"] == 0
    assert tree() == before, "passive identify changed files under HOME"
