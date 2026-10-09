"""Tests for the BOD-297 credential-pool identity helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.credential_pools import (
    ALIAS_FAMILIES,
    base_route,
    canonical_route,
    collapse_alias_duplicates,
    is_non_chat_route,
    pool_of,
)
from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.provider_catalog import backend_pool
from verdict.prove_at_rest import (
    AdmittedRoute,
    ProbeExchange,
    Prober,
    census_report,
    order_cycle,
    routes_from_evidence,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _route(
    route_id: str, capacity: str, *, pool: str | None = None, non_chat: bool = False
) -> AdmittedRoute:
    return AdmittedRoute(
        route_id=route_id,
        provider=route_id.split("/", 1)[0],
        capacity=capacity,
        pool=pool,
        non_chat=non_chat,
    )


@pytest.mark.parametrize(
    "route_id",
    [
        "agy/claude-opus-4-6",
        "antigravity/claude-opus-4-6",
        "kc/model:free",
        "kilocode/model:free",
        "openrouter/model:free",
    ],
)
def test_pool_of_matches_backend_pool_for_shared_families(route_id: str) -> None:
    """Where provider_catalog.backend_pool already has an answer, agree with it.

    eligibility.py (reviewer independence) and this census planner must never
    disagree about which families share a quota.
    """
    assert pool_of(route_id) == backend_pool(route_id)


def test_pool_of_delegates_before_falling_back_to_extra_table() -> None:
    # kc/model (no :free) is not covered by backend_pool's :free rule, so
    # pool_of falls back to this module's extra table (kc -> kilocode),
    # rather than backend_pool's bare-prefix default (kc -> "kc").
    assert backend_pool("kc/model") == "kc"
    assert pool_of("kc/model") == "kilocode"


def test_pool_of_collapses_known_extra_alias_pairs() -> None:
    pairs = [
        ("af", "api-airforce"),
        ("ollama-cloud", "ollama-cloud"),
        ("ollamacloud", "ollama-cloud"),
        ("bm", "bluesminds"),
        ("gh", "github"),
        ("cc", "claude"),
        ("cx", "codex"),
        ("oc", "opencode"),
        ("zm", "zenmux"),
        ("sambanova", "sambanova"),
        ("samba", "sambanova"),
        ("dv", "devin-cli"),
        ("dva", "devin-cli"),
    ]
    for prefix, pool in pairs:
        assert pool_of(f"{prefix}/model-x") == pool


def test_pool_of_is_case_insensitive_on_prefix() -> None:
    assert pool_of("AGY/model") == pool_of("agy/model")


def test_pool_of_unaliased_prefix_is_its_own_pool() -> None:
    assert pool_of("openrouter/some-model") == "openrouter"
    assert pool_of("nvidia/nvidia/llama") == "nvidia"


def test_cursor_family_stays_split_from_cursor_api_family() -> None:
    # cursor (oauth) and cursor-api (apikey) are distinct connections with
    # no shared-quota evidence; they must not collapse onto one pool.
    assert pool_of("cu/model") == "cursor"
    assert pool_of("cursor/model") == "cursor"
    assert pool_of("cua/model") == "cursor-api"
    assert pool_of("cursor-api/model") == "cursor-api"
    assert pool_of("cu/model") != pool_of("cua/model")


def test_devin_desktop_is_not_in_the_alias_table() -> None:
    # No evidence ties any route to devin-desktop; it is deliberately left
    # as its own unaliased pool rather than guessed into the dv/dva family.
    assert "devin-desktop" not in ALIAS_FAMILIES
    assert pool_of("devin-desktop/model") == "devin-desktop"


def test_canonical_route_rewrites_only_the_prefix() -> None:
    assert canonical_route("agy/claude-opus-4-6-thinking") == "antigravity/claude-opus-4-6-thinking"
    assert canonical_route("af/qwen3-coder:free") == "api-airforce/qwen3-coder:free"


def test_canonical_route_is_identity_for_unaliased_prefix() -> None:
    assert canonical_route("openrouter/auto") == "openrouter/auto"


def test_canonical_route_handles_bare_id_without_slash() -> None:
    assert canonical_route("gh") == "github"


def test_base_route_strips_plain_effort_suffix_when_base_known() -> None:
    inventory = {"kr/claude-sonnet-5-thinking", "kr/claude-sonnet-5-thinking-low"}
    assert base_route("kr/claude-sonnet-5-thinking-low", inventory) == "kr/claude-sonnet-5-thinking"


def test_base_route_strips_thinking_effort_suffix_when_base_known() -> None:
    inventory = {"agy/claude-opus-4-6", "agy/claude-opus-4-6-thinking-high"}
    assert base_route("agy/claude-opus-4-6-thinking-high", inventory) == "agy/claude-opus-4-6"


def test_base_route_prefers_thinking_strip_when_both_candidates_exist() -> None:
    inventory = {
        "agy/claude-opus-4-6",
        "agy/claude-opus-4-6-thinking",
        "agy/claude-opus-4-6-thinking-high",
    }
    assert base_route("agy/claude-opus-4-6-thinking-high", inventory) == "agy/claude-opus-4-6"


def test_base_route_unchanged_when_no_candidate_is_known() -> None:
    inventory = {"cu/claude-opus-5-thinking-high"}
    assert (
        base_route("cu/claude-opus-5-thinking-high", inventory) == "cu/claude-opus-5-thinking-high"
    )


def test_base_route_unchanged_for_id_without_effort_suffix() -> None:
    inventory = {"openrouter/auto"}
    assert base_route("openrouter/auto", inventory) == "openrouter/auto"


def test_base_route_accepts_any_collection_type() -> None:
    inventory_list = ["kr/model", "kr/model-low"]
    assert base_route("kr/model-low", inventory_list) == "kr/model"


def test_routes_from_evidence_sets_pool_from_pool_of(tmp_path: Path) -> None:
    """routes_from_evidence must not hard-code pool=None; it reuses pool_of."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    inventory = [
        {"id": "agy/model-a", "owned_by": "agy", "capabilities": {"tool_calling": True}},
        {"id": "af/model-b", "owned_by": "api-airforce", "capabilities": {"tool_calling": True}},
    ]
    connections = [
        {
            "provider": "agy",
            "authType": "oauth",
            "isActive": True,
            "testStatus": "active",
            "providerSpecificData": {"tier": "free-tier", "plan": "Antigravity starter quota"},
        },
        {
            "provider": "api-airforce",
            "authType": "apikey",
            "isActive": True,
            "testStatus": "active",
            "providerSpecificData": {"importFreeModelsOnly": True},
        },
    ]
    routes = routes_from_evidence(inventory, connections, now=now, state_dir=tmp_path)
    by_id = {route.route_id: route for route in routes}
    assert by_id["agy/model-a"].pool == pool_of("agy/model-a")
    assert by_id["af/model-b"].pool == pool_of("af/model-b") == "api-airforce"


def test_collapse_alias_duplicates_keeps_canonical_member() -> None:
    kept, inherited = collapse_alias_duplicates(["agy/model-x", "antigravity/model-x"])
    assert kept == ["antigravity/model-x"]
    assert inherited == {"agy/model-x": "antigravity/model-x"}


def test_collapse_alias_duplicates_is_a_noop_for_unaliased_routes() -> None:
    kept, inherited = collapse_alias_duplicates(["openrouter/a", "nvidia/b"])
    assert kept == ["openrouter/a", "nvidia/b"]
    assert inherited == {}


def test_is_non_chat_route_flags_known_embedding_marker() -> None:
    assert is_non_chat_route("af/BAAI/bge-reranker-v2-m3") is True
    assert is_non_chat_route("agy/claude-opus-4-6") is False


def test_is_non_chat_route_uses_row_type_and_capabilities() -> None:
    assert is_non_chat_route("x/y", {"type": "embedding"}) is True
    assert is_non_chat_route("x/y", {"capabilities": {"tts": True}}) is True
    assert is_non_chat_route("x/y", {"capabilities": {"tool_calling": True}}) is False


def test_order_cycle_collapses_alias_duplicates_to_one_probe(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route("agy/model-x", "free"), _route("antigravity/model-x", "free")]
    ordered = [route.route_id for route, _kind in order_cycle(routes, cache, NOW, epsilon=0)]
    assert ordered == ["antigravity/model-x"]


def test_order_cycle_skips_non_chat_routes_entirely(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route("af/bge-reranker", "free", non_chat=True), _route("af/chat-model", "free")]
    ordered = [route.route_id for route, _kind in order_cycle(routes, cache, NOW, epsilon=0)]
    assert ordered == ["af/chat-model"]


def test_order_cycle_defers_effort_variant_until_base_is_healthy(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route("kr/model-thinking", "free"), _route("kr/model-thinking-high", "free")]
    first = [route.route_id for route, _kind in order_cycle(routes, cache, NOW, epsilon=0)]
    assert first == ["kr/model-thinking"]

    from verdict.orchestration.health_cache import CATEGORY_OK, ProbeResult

    cache.record(
        "kr/model-thinking", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW
    )
    second = [
        route.route_id
        for route, _kind in order_cycle(routes, cache, NOW + timedelta(seconds=1), epsilon=0)
    ]
    assert second == ["kr/model-thinking-high"]


def _auth_401(route_id: str, phase: str, timeout: float) -> ProbeExchange:
    return ProbeExchange(http_status=401, ok=False, error_category="auth")


def test_auth_outage_breaker_trips_on_five_failures_across_three_pools(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [
        _route("p1/a", "free", pool="p1"),
        _route("p2/a", "free", pool="p2"),
        _route("p3/a", "free", pool="p3"),
        _route("p1/b", "free", pool="p1"),
        _route("p2/b", "free", pool="p2"),
    ]
    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=_auth_401,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    stats = prober.run_once()
    assert stats.auth_outage is True
    assert stats.stopped_reason == "auth_outage"
    # No negative was recorded for any of the 5 routes: the breaker
    # discards the buffered outage-window failures unwritten.
    for route in routes:
        assert cache.entry(route.route_id) is None


def test_auth_outage_breaker_does_not_trip_under_pool_threshold(tmp_path: Path) -> None:
    """Five 401s from only two distinct pools are ordinary negatives, not an outage."""
    cache = HealthCache(tmp_path / "cache.json")
    routes = [
        _route("p1/a", "free", pool="p1"),
        _route("p1/b", "free", pool="p1"),
        _route("p1/c", "free", pool="p1"),
        _route("p2/a", "free", pool="p2"),
        _route("p2/b", "free", pool="p2"),
    ]
    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=_auth_401,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    stats = prober.run_once()
    assert stats.auth_outage is False
    assert stats.stopped_reason != "auth_outage"
    for route in routes:
        assert cache.entry(route.route_id) is not None


def test_auth_outage_breaker_flushes_buffer_as_negatives_once_streak_breaks(tmp_path: Path) -> None:
    """Fewer than the threshold, then a success: every buffered 401 writes as a real negative."""
    cache = HealthCache(tmp_path / "cache.json")
    routes = [
        _route("p1/a", "free", pool="p1"),
        _route("p2/a", "free", pool="p2"),
        _route("p3/a", "free", pool="p3"),
        _route("p4/ok", "free", pool="p4"),
    ]

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        if route_id == "p4/ok":
            return ProbeExchange(http_status=200, ok=True, chat_exact=True, latency_ms=5)
        return ProbeExchange(http_status=401, ok=False, error_category="auth")

    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    stats = prober.run_once()
    assert stats.auth_outage is False
    for route in routes[:3]:
        entry = cache.entry(route.route_id)
        assert entry is not None and not entry.healthy


def test_census_report_groups_by_canonical_pool_and_tallies_usable(tmp_path: Path) -> None:
    from verdict.orchestration.health_cache import CATEGORY_OK, CATEGORY_PAYMENT, ProbeResult

    cache = HealthCache(tmp_path / "cache.json")
    cache.record(
        "antigravity/model-x", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW
    )
    cache.record(
        "af/model-y", ProbeResult(category=CATEGORY_PAYMENT, chat_ok=False, tool_ok=False), NOW
    )
    routes = [
        _route("agy/model-x", "free", pool="antigravity"),
        _route("antigravity/model-x", "free", pool="antigravity"),
        _route("af/model-y", "free", pool="api-airforce"),
        _route("af/bge-reranker", "free", pool="api-airforce", non_chat=True),
    ]
    report = census_report(routes, cache, now=NOW)
    assert report["non_chat_skipped"] == 1
    assert report["inherited"] == {"agy/model-x": "antigravity/model-x"}
    pools = report["pools"]
    assert pools["antigravity"]["routes"] == 2
    assert pools["antigravity"]["inherited_from_alias"] == 1
    assert pools["antigravity"]["usable"] == 1
    assert pools["api-airforce"]["unusable"] == {"payment_required": 1}
    assert pools["api-airforce"]["usable"] == 0


def test_census_report_is_read_only_and_never_writes_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache = HealthCache(cache_path)
    routes = [_route("openrouter/model-a", "unknown", pool="openrouter")]
    census_report(routes, cache, now=NOW)
    assert not cache_path.exists(), "census_report must never call cache.save()"


def test_census_action_reads_offline_inventory_file_and_cache(tmp_path: Path) -> None:
    import json

    from verdict.actions.registry import run_action

    inventory_file = tmp_path / "routes.json"
    inventory_file.write_text(
        json.dumps(
            [
                {"route_id": "openrouter/model-a", "provider": "openrouter", "capacity": "free"},
                {
                    "route_id": "af/bge-reranker",
                    "provider": "af",
                    "capacity": "free",
                    "non_chat": True,
                },
            ]
        )
    )
    result = run_action(
        "prove-at-rest.census",
        {"inventory_path": str(inventory_file), "state_path": str(tmp_path / "cache.json")},
    )
    assert result.ok
    assert result.data["non_chat_skipped"] == 1
    assert "openrouter" in result.data["pools"]
    assert not (tmp_path / "cache.json").exists(), "the census action must never write the cache"


def test_census_action_requires_inventory_path() -> None:
    from verdict.actions.registry import run_action

    result = run_action("prove-at-rest.census", {})
    assert not result.ok
    assert "inventory_path" in result.data["error"]


@pytest.mark.parametrize("capacity", ["free", "subscription"])
def test_default_epsilon_does_not_probe_deferred_effort(tmp_path: Path, capacity: str) -> None:
    """Reviewer repro: epsilon must not re-add p/base-high before its base passes."""
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route("p/base", capacity), _route("p/base-high", capacity)]
    assert [route.route_id for route, _kind in order_cycle(routes, cache, NOW)] == ["p/base"]


def test_default_epsilon_keeps_alias_and_non_chat_filters(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [
        _route("agy/model", "free"),
        _route("antigravity/model", "free"),
        _route("af/bge-reranker", "free", non_chat=True),
    ]
    assert [route.route_id for route, _kind in order_cycle(routes, cache, NOW)] == [
        "antigravity/model"
    ]


def test_main_auth_outage_skips_agentic_without_poisoning_cache(tmp_path: Path) -> None:
    """Reviewer repro: five main-phase 401s must prevent every agentic HTTP call."""
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route(f"p{i}/model", "free", pool=f"p{i}") for i in range(5)]
    calls: list[str] = []

    def agentic(route_id: str, payload: object, timeout: float) -> ProbeExchange:
        calls.append(route_id)
        return ProbeExchange(http_status=403, ok=False, error_category="permission")

    stats = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=_auth_401,
        agentic_transport=agentic,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    ).run_once()
    assert stats.auth_outage
    assert calls == []
    assert stats.probed == stats.negative == 0
    assert cache.routes() == {}
    assert HealthCache(cache.path).routes() == {}


@pytest.mark.parametrize("count", [4, 5])
def test_agentic_auth_failures_use_the_same_outage_buffer(tmp_path: Path, count: int) -> None:
    from verdict.prove_at_rest import CycleStats

    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route(f"p{i}/model", "free", pool=f"p{i}") for i in range(count)]
    calls: list[str] = []

    def agentic(route_id: str, payload: object, timeout: float) -> ProbeExchange:
        calls.append(route_id)
        return ProbeExchange(http_status=403, ok=False, error_category="permission")

    prober = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=_auth_401,
        agentic_transport=agentic,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert stats.auth_outage == (count == 5)
    assert len(calls) == count
    if count == 5:
        assert stats.stopped_reason == "auth_outage"
        assert stats.probed == stats.negative == 0
        assert cache.routes() == {}
    else:
        assert stats.probed == stats.negative == count
        for route in routes:
            entry = HealthCache(cache.path).entry(route.route_id)
            assert entry is not None and entry.category == "permission"
            assert entry.agentic_checked_at == NOW


def test_auth_outage_streak_spans_main_and_agentic_phases(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    routes = [_route(f"p{i}/model", "free", pool=f"p{i}") for i in range(4)]
    calls: list[str] = []

    def agentic(route_id: str, payload: object, timeout: float) -> ProbeExchange:
        calls.append(route_id)
        return ProbeExchange(http_status=403, ok=False, error_category="permission")

    stats = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=_auth_401,
        agentic_transport=agentic,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    ).run_once()
    assert stats.auth_outage
    assert calls == [routes[0].route_id]
    assert cache.routes() == {}


@pytest.mark.parametrize("direct", [True, False])
def test_agentic_phase_uses_the_same_filtered_plan(tmp_path: Path, direct: bool) -> None:
    """Reviewer repro: aliases and non-chat routes never get agentic cache entries."""
    from verdict.prove_at_rest import CycleStats

    cache = HealthCache(tmp_path / "cache.json")
    routes = [
        _route("agy/model", "free"),
        _route("antigravity/model", "free"),
        _route("af/bge-reranker", "free", non_chat=True),
        _route("p/base", "free"),
        _route("p/base-high", "free"),
    ]
    calls: list[str] = []
    loads: list[bool] = []

    def loader() -> list[AdmittedRoute]:
        loads.append(True)
        return routes

    def agentic(route_id: str, payload: object, timeout: float) -> ProbeExchange:
        calls.append(route_id)
        return ProbeExchange(http_status=403, ok=False, error_category="permission")

    prober = Prober(
        cache=cache,
        routes_loader=loader,
        transport=_auth_401,
        agentic_transport=agentic,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    if direct:
        prober.run_agentic_probes(CycleStats())
    else:
        prober.run_once()
    assert calls == ["antigravity/model", "p/base"]
    assert len(loads) == 1, "one loader snapshot must serve both phases"
    assert set(cache.routes()) == set(calls)


def test_alias_routes_consume_one_canonical_pool_bucket(tmp_path: Path) -> None:
    from verdict.orchestration.health_cache import bucket_key

    pool = pool_of("agy/model-a")
    assert pool == pool_of("antigravity/model-b")
    assert bucket_key("agy", pool) == bucket_key("antigravity", pool) == pool
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=1)
    assert cache.consume("agy", NOW, pool=pool)
    assert not cache.consume("antigravity", NOW, pool=pool)
    assert cache.bucket_remaining("antigravity", NOW, pool=pool) == 0
    cache.save()
    assert not HealthCache(cache.path).consume("antigravity", NOW, pool=pool)


def test_prober_alias_routes_draw_from_one_bucket(tmp_path: Path) -> None:
    pool = pool_of("agy/model-a")
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=1)
    routes = [
        _route("agy/model-a", "free", pool=pool),
        _route("antigravity/model-b", "free", pool=pool),
    ]
    calls: list[str] = []

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append(route_id)
        return _auth_401(route_id, phase, timeout)

    stats = Prober(
        cache=cache,
        routes_loader=lambda: routes,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    ).run_once()
    assert calls == ["agy/model-a"]
    assert stats.skipped_bucket == 1


def test_round_robin_groups_aliases_by_canonical_pool(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json")
    pool = pool_of("agy/model-a")
    routes = [
        _route("agy/model-a", "free", pool=pool),
        _route("antigravity/model-b", "free", pool=pool),
        _route("x/model", "free", pool="x"),
    ]
    assert [route.route_id for route, _kind in order_cycle(routes, cache, NOW)] == [
        "agy/model-a",
        "x/model",
        "antigravity/model-b",
    ]


def test_census_human_output_shows_unusable_count_and_top_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from verdict import cli, present
    from verdict.orchestration.health_cache import CATEGORY_PAYMENT, ProbeResult

    inventory = tmp_path / "routes.json"
    inventory.write_text(
        json.dumps([{"route_id": f"p/m{i}", "provider": "p", "capacity": "free"} for i in range(2)])
    )
    cache = HealthCache(tmp_path / "cache.json")
    cache.record(
        "p/m0",
        ProbeResult(category=CATEGORY_PAYMENT, chat_ok=False, tool_ok=False),
        datetime.now(timezone.utc),
    )
    cache.save()
    tables: list[tuple[object, object]] = []
    notes: list[str] = []
    monkeypatch.setattr(present, "header", lambda _title: None)
    monkeypatch.setattr(present, "table", lambda headers, rows: tables.append((headers, rows)))
    monkeypatch.setattr(present, "note", notes.append)
    cli.cmd_prove_at_rest("census", inventory_path=str(inventory), state_path=str(cache.path))
    headers, rows = tables[0]
    assert "Unusable" in headers
    assert rows[0][headers.index("Unusable")] == 1
    assert any("payment_required: 1" in note for note in notes)
