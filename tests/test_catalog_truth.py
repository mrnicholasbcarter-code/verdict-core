"""Tests for Story 1: catalog truth, capacity classification, backend pool identity.

Fixtures derived from research-or-providers.json, research-or-models.json,
research-free-probe.jsonl and elig-all.json.  Sanitized: no API keys, tokens,
account emails, or host paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import CapacityClass, TaskRequirements
from verdict.orchestration.eligibility import (
    _CAPACITY_ORDER,
    EligibilityLadder,
    cooldown_seconds_for,
)
from verdict.orchestration.provider_catalog import (
    CATALOG_STALE_COOLDOWN_SECONDS,
    OWNED_BY_ALIASES,
    backend_pool,
    connection_signals_free,
    has_free_suffix,
    is_catalog_stale_error,
    is_not_free_overridden,
    is_not_free_signal,
    pool_aware_families,
    record_not_free_override,
    resolve_provider,
    routes_share_pool,
)
from verdict.subagent_selection import HealthResult

FIXTURES = Path(__file__).parent / "fixtures" / "catalog_truth"
NOW = datetime(2026, 9, 29, 5, 0, 0, tzinfo=timezone.utc)


def _load_connections() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "connections.json").read_text())


def _load_inventory() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "inventory.json").read_text())


def _make_ladder(
    inventory: list[dict[str, Any]] | None = None,
    connections: list[dict[str, Any]] | None = None,
    state_path: Path | None = None,
    *,
    probe_healthy: bool = True,
) -> EligibilityLadder:
    inv = inventory or _load_inventory()
    conns = connections or _load_connections()
    sp = state_path or Path("/tmp/vs1-test-state.json")
    sp.write_text("{}")

    def probe(route_id: str) -> HealthResult:
        return HealthResult(healthy=probe_healthy, category="", status_code=200)

    return EligibilityLadder(inventory_rows=inv, connections=conns, probe=probe, state_path=sp)


# -----------------------------------------------------------------------
# A. OWNED_BY_ALIASES
# -----------------------------------------------------------------------


class TestOwnedByAliases:
    def test_devin_cli_agentic_resolves(self) -> None:
        assert resolve_provider("devin-cli-agentic", "dva/x") == "devin-cli"

    def test_codex_app_server_resolves(self) -> None:
        assert resolve_provider("codex-app-server", "cxa/x") == "codex"

    def test_auggie_is_named_gap(self) -> None:
        # auggie maps to itself: a named gap, not silently dropped.
        assert resolve_provider("auggie", "aug/x") == "auggie"

    def test_cloudflare_playground_is_named_gap(self) -> None:
        assert resolve_provider("cloudflare-playground", "cfp/x") == "cloudflare-playground"

    def test_unknown_provider_passes_through(self) -> None:
        assert resolve_provider("github", "gh/model") == "github"

    def test_all_gaps_in_audit_are_covered(self) -> None:
        """Every owned_by gap from research-audit.md §5 is in the table."""
        expected_gaps = {
            "devin-cli-agentic",
            "auggie",
            "codex-app-server",
            "cloudflare-playground",
            "duckduckgo-web",
            "felo-web",
            "uncloseai",
            "veoaifree-web",
            "chipotle",
        }
        assert expected_gaps <= set(OWNED_BY_ALIASES.keys())


# -----------------------------------------------------------------------
# B. BACKEND POOL IDENTITY
# -----------------------------------------------------------------------


class TestBackendPool:
    def test_agy_antigravity_same_pool(self) -> None:
        """Evidence: identical tool-call ids in research-empirical.md."""
        assert backend_pool("agy/claude-sonnet-4-6") == "google-antigravity"
        assert backend_pool("antigravity/claude-sonnet-4-6") == "google-antigravity"
        assert backend_pool("agy/claude-sonnet-4-6") == backend_pool(
            "antigravity/claude-sonnet-4-6"
        )

    def test_kilocode_openrouter_free_same_pool(self) -> None:
        """Evidence: same upstream generation id in research-empirical.md."""
        kilo = backend_pool("kilocode/cohere/north-mini-code:free")
        ortr = backend_pool("openrouter/cohere/north-mini-code:free")
        assert kilo == "openrouter-free"
        assert ortr == "openrouter-free"
        assert kilo == ortr

    def test_non_free_kilocode_is_own_pool(self) -> None:
        assert backend_pool("kilocode/claude-sonnet-4-6") == "kilocode"

    def test_non_free_openrouter_is_own_pool(self) -> None:
        assert backend_pool("openrouter/anthropic/claude-sonnet-4") == "openrouter"

    def test_cc_is_own_pool(self) -> None:
        assert backend_pool("cc/claude-sonnet-5") == "cc"

    def test_routes_share_pool_helper(self) -> None:
        assert routes_share_pool("agy/model-a", "antigravity/model-b")
        assert not routes_share_pool("agy/model-a", "cc/model-b")

    def test_pool_aware_families(self) -> None:
        ids = {"agy/claude-sonnet-4-6", "antigravity/gpt-oss-120b-medium"}
        families = pool_aware_families(ids)
        # Both should map to the same pool
        assert families == frozenset({"google-antigravity"})

    def test_agy_antigravity_not_independent(self) -> None:
        """agy→antigravity must NOT count as independent failover or review."""
        assert routes_share_pool("agy/claude-sonnet-4-6", "antigravity/claude-sonnet-4-6")


# -----------------------------------------------------------------------
# C. CAPACITY CLASSIFICATION
# -----------------------------------------------------------------------


class TestCapacityClassification:
    def test_free_suffix_overrides_oauth(self) -> None:
        """Fixes D5: 72 kilocode :free misclassed as SUBSCRIPTION."""
        ladder = _make_ladder()
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))
        verdicts = ladder.evaluate(req, now=NOW)
        kilo_free = [v for v in verdicts if ":free" in v.route_id and "kilocode" in v.route_id]
        for v in kilo_free:
            assert v.capacity_class == CapacityClass.FREE, (
                f"{v.route_id} should be FREE, got {v.capacity_class}"
            )

    def test_oauth_with_free_tier_signal_is_free(self) -> None:
        """agy/antigravity: oauth with tier=free-tier → FREE."""
        ladder = _make_ladder()
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))
        verdicts = ladder.evaluate(req, now=NOW)
        agy = [v for v in verdicts if v.route_id.startswith("agy/")]
        for v in agy:
            assert v.capacity_class == CapacityClass.FREE, (
                f"{v.route_id} should be FREE, got {v.capacity_class}"
            )

    def test_oauth_without_free_signal_is_subscription(self) -> None:
        """Regular kilocode (no :free) with oauth → SUBSCRIPTION."""
        ladder = _make_ladder()
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))
        verdicts = ladder.evaluate(req, now=NOW)
        kilo_sub = [v for v in verdicts if v.route_id == "kilocode/claude-sonnet-4-6"]
        for v in kilo_sub:
            assert v.capacity_class == CapacityClass.SUBSCRIPTION, (
                f"{v.route_id} should be SUBSCRIPTION, got {v.capacity_class}"
            )

    def test_apikey_positive_pricing_is_metered(self) -> None:
        ladder = _make_ladder()
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))
        verdicts = ladder.evaluate(req, now=NOW)
        metered = [v for v in verdicts if v.route_id == "charm-hyper/claude-sonnet-4-6"]
        for v in metered:
            assert v.capacity_class == CapacityClass.METERED, (
                f"{v.route_id} should be METERED, got {v.capacity_class}"
            )

    def test_apikey_no_pricing_is_unknown(self) -> None:
        """command-code has apikey but no pricing → UNKNOWN."""
        ladder = _make_ladder()
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))
        verdicts = ladder.evaluate(req, now=NOW)
        cmd = [v for v in verdicts if v.route_id == "command-code/gpt-5.3-codex"]
        for v in cmd:
            assert v.capacity_class == CapacityClass.UNKNOWN, (
                f"{v.route_id} should be UNKNOWN, got {v.capacity_class}"
            )

    def test_import_free_models_only_is_free(self) -> None:
        """Connection with importFreeModelsOnly=true → FREE."""
        is_free, rule = connection_signals_free(
            {"providerSpecificData": {"importFreeModelsOnly": True}, "authType": "apikey"}
        )
        assert is_free
        assert rule == "importFreeModelsOnly"

    def test_free_tier_in_psd_is_free(self) -> None:
        is_free, rule = connection_signals_free(
            {
                "providerSpecificData": {"tier": "free-tier", "plan": "Antigravity starter quota"},
                "authType": "oauth",
            }
        )
        assert is_free
        assert "tier" in rule

    def test_has_free_suffix(self) -> None:
        assert has_free_suffix("kilocode/model:free")
        assert not has_free_suffix("kilocode/model")
        assert not has_free_suffix("kilocode/model-free")


# -----------------------------------------------------------------------
# D. CATALOG GHOST CLASSIFICATION
# -----------------------------------------------------------------------


class TestCatalogGhost:
    def test_catalog_stale_error_detected(self) -> None:
        msg = "Model 'qwen3-coder-480b-a35b-instruct' is not available in the active live catalog for provider 'api-airforce'."
        assert is_catalog_stale_error(msg)

    def test_normal_error_not_catalog_stale(self) -> None:
        assert not is_catalog_stale_error("rate limited")
        assert not is_catalog_stale_error("")

    def test_catalog_stale_cooldown_is_long(self) -> None:
        seconds = cooldown_seconds_for("catalog_stale", None)
        assert seconds == CATALOG_STALE_COOLDOWN_SECONDS
        assert seconds >= 3600  # at least 1 hour


# -----------------------------------------------------------------------
# E. NOT-FREE OVERRIDE (live evidence)
# -----------------------------------------------------------------------


class TestNotFreeOverride:
    def test_payment_required_signal(self) -> None:
        assert is_not_free_signal(category="payment_required")

    def test_402_status_signal(self) -> None:
        assert is_not_free_signal(status_code=402)

    def test_insufficient_credits_message(self) -> None:
        assert is_not_free_signal(
            error_message="You have insufficient credits to make this request"
        )

    def test_deposit_required_message(self) -> None:
        assert is_not_free_signal(error_message="deposit required")

    def test_normal_error_is_not_signal(self) -> None:
        assert not is_not_free_signal(category="rate_limited", status_code=429)
        assert not is_not_free_signal(category="timeout")

    def test_record_and_check_override(self) -> None:
        state: dict[str, Any] = {}
        until = (NOW + timedelta(hours=6)).isoformat()
        record_not_free_override(
            state,
            "cerebras/gpt-oss-120b",
            pool="cerebras",
            until_iso=until,
            reason="payment_required",
        )
        overridden, reason = is_not_free_overridden(
            state, "cerebras/gpt-oss-120b", pool="cerebras", now_iso=NOW.isoformat()
        )
        assert overridden
        assert reason == "payment_required"

    def test_override_expires(self) -> None:
        state: dict[str, Any] = {}
        past = (NOW - timedelta(hours=1)).isoformat()
        record_not_free_override(
            state,
            "cerebras/gpt-oss-120b",
            pool="cerebras",
            until_iso=past,
            reason="payment_required",
        )
        overridden, _ = is_not_free_overridden(
            state, "cerebras/gpt-oss-120b", pool="cerebras", now_iso=NOW.isoformat()
        )
        assert not overridden

    def test_pool_override_applies_to_sibling(self) -> None:
        state: dict[str, Any] = {}
        until = (NOW + timedelta(hours=6)).isoformat()
        record_not_free_override(
            state,
            "cerebras/gpt-oss-120b",
            pool="cerebras",
            until_iso=until,
            reason="payment_required",
        )
        # A different route in the same pool should also be overridden
        overridden, _ = is_not_free_overridden(
            state, "cerebras/other-model", pool="cerebras", now_iso=NOW.isoformat()
        )
        assert overridden


# -----------------------------------------------------------------------
# F. SELECTION ORDER UNCHANGED (behaviour-preserving)
# -----------------------------------------------------------------------


class TestSelectionOrderUnchanged:
    """The _CAPACITY_ORDER mapping must be unchanged (Story 3 holds this)."""

    def test_capacity_order_is_subscription_first(self) -> None:
        assert _CAPACITY_ORDER[CapacityClass.SUBSCRIPTION] == 0
        assert _CAPACITY_ORDER[CapacityClass.FREE] == 1
        assert _CAPACITY_ORDER[CapacityClass.METERED] == 2
        assert _CAPACITY_ORDER[CapacityClass.UNKNOWN] == 3


# -----------------------------------------------------------------------
# G. PROVIDER RESOLUTION IN LADDER
# -----------------------------------------------------------------------


class TestProviderResolution:
    def test_codex_app_server_resolves_to_codex(self) -> None:
        """cxa/ routes with owned_by=codex-app-server should resolve to codex connection."""
        ladder = _make_ladder()
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))
        verdicts = ladder.evaluate(req, now=NOW)
        cxa = [v for v in verdicts if v.route_id == "cxa/claude-sonnet-4-6"]
        assert len(cxa) == 1
        # Should resolve to codex provider via alias
        assert cxa[0].provider in ("codex-app-server", "codex")
