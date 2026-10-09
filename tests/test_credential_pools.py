"""Tests for the BOD-297 credential-pool identity helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.orchestration.credential_pools import (
    ALIAS_FAMILIES,
    base_route,
    canonical_route,
    pool_of,
)
from verdict.orchestration.provider_catalog import backend_pool
from verdict.prove_at_rest import routes_from_evidence


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
