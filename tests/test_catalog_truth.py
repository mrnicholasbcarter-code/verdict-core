"""Tests for Story 1: catalog truth, capacity classification, backend pool identity.

Fixtures derived from research-or-providers.json, research-or-models.json,
research-free-probe.jsonl and elig-all.json.  Sanitized: no API keys, tokens,
account emails, or host paths.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import CapacityClass, TaskRequirements
from verdict.orchestration.eligibility import (
    _CAPACITY_ORDER,
    EligibilityLadder,
    cooldown_seconds_for,
)
from verdict.orchestration.provider_catalog import (
    CATALOG_STALE_COOLDOWN_SECONDS,
    OWNED_BY_ALIASES,
    aliased_pools_for,
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
        assert resolve_provider("devin-cli-agentic") == "devin-cli"

    def test_codex_app_server_resolves(self) -> None:
        assert resolve_provider("codex-app-server") == "codex"

    def test_auggie_is_named_gap(self) -> None:
        # auggie maps to itself: a named gap, not silently dropped.
        assert resolve_provider("auggie") == "auggie"

    def test_cloudflare_playground_is_named_gap(self) -> None:
        assert resolve_provider("cloudflare-playground") == "cloudflare-playground"

    def test_unknown_provider_passes_through(self) -> None:
        assert resolve_provider("github") == "github"

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

    def test_kilocode_aliases_and_openrouter_free_share_pool(self) -> None:
        """Evidence: same upstream generation id in research-empirical.md."""
        short = backend_pool("kc/cohere/north-mini-code:free")
        kilo = backend_pool("kilocode/cohere/north-mini-code:free")
        ortr = backend_pool("openrouter/cohere/north-mini-code:free")
        assert short == kilo == ortr == "openrouter-free"

    def test_kc_free_route_is_not_independent_from_openrouter_free(self) -> None:
        worker = "kc/cohere/north-mini-code:free"
        reviewer = "openrouter/cohere/north-mini-code:free"
        assert routes_share_pool(worker, reviewer)
        assert aliased_pools_for({worker}) == frozenset({"openrouter-free"})

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

    def test_aliased_pools_for_cross_prefix_only(self) -> None:
        assert aliased_pools_for({"agy/claude-sonnet-4-6"}) == frozenset({"google-antigravity"})
        assert aliased_pools_for({"cc/claude-sonnet-5"}) == frozenset()
        assert aliased_pools_for({"kilocode/model:free"}) == frozenset({"openrouter-free"})


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

    def test_import_free_only_snake_case_is_free(self) -> None:
        """Sanitized connections (and existing tests) use import_free_only."""
        conn = {"import_free_only": True, "authType": "apikey", "plan_label": "payg"}
        is_free, rule = connection_signals_free(conn)
        assert is_free
        assert rule == "import_free_only"
        row = {
            "id": "gl/glm-5",
            "owned_by": "glm",
            "capabilities": {"tool_calling": True},
            "context_length": 128000,
            "pricing": {"input": 1.0, "output": 2.0},
        }
        ladder = _make_ladder(
            inventory=[row],
            connections=[{**conn, "provider": "glm", "isActive": True, "testStatus": "ok"}],
        )
        capacity, _plan, evidence = ladder._capacity_class(conn, row, route_id="gl/glm-5")
        assert capacity is CapacityClass.FREE
        assert evidence == "import_free_only"
        verdicts = ladder.evaluate(
            TaskRequirements(required_capabilities=frozenset({"tools"})), now=NOW
        )
        assert verdicts[0].capacity_class == CapacityClass.FREE

    def test_apikey_plan_label_free_is_free(self) -> None:
        """plan_label containing 'free' is FREE for any auth type, not just oauth."""
        conn = {"authType": "apikey", "plan_label": "free", "import_free_only": False}
        is_free, rule = connection_signals_free(conn)
        assert is_free
        assert rule == "plan_label"
        row = {
            "id": "pay/model",
            "owned_by": "pay",
            "capabilities": {"tool_calling": True},
            "context_length": 128000,
            "pricing": {"input": 3.0, "output": 15.0},
        }
        ladder = _make_ladder(
            inventory=[row],
            connections=[{**conn, "provider": "pay", "isActive": True, "testStatus": "ok"}],
        )
        capacity, _plan, evidence = ladder._capacity_class(conn, row, route_id="pay/model")
        assert capacity is CapacityClass.FREE
        assert evidence == "plan_label"
        verdicts = ladder.evaluate(
            TaskRequirements(required_capabilities=frozenset({"tools"})), now=NOW
        )
        assert verdicts[0].capacity_class == CapacityClass.FREE

    def test_all_zero_pricing_is_free(self) -> None:
        """All-zero explicit pricing is FREE even for apikey without other signals."""
        conn = {
            "provider": "zero",
            "authType": "apikey",
            "isActive": True,
            "testStatus": "ok",
            "plan_label": "payg",
            "import_free_only": False,
        }
        row = {
            "id": "zero/model",
            "owned_by": "zero",
            "capabilities": {"tool_calling": True},
            "context_length": 128000,
            "pricing": {"input": 0, "output": 0},
        }
        ladder = _make_ladder(inventory=[row], connections=[conn])
        capacity, _plan, evidence = ladder._capacity_class(conn, row, route_id="zero/model")
        assert capacity is CapacityClass.FREE
        assert evidence == "all_zero_pricing"
        verdicts = ladder.evaluate(
            TaskRequirements(required_capabilities=frozenset({"tools"})), now=NOW
        )
        assert verdicts[0].capacity_class == CapacityClass.FREE

    def test_free_tier_in_psd_is_free(self) -> None:
        is_free, rule = connection_signals_free(
            {
                "providerSpecificData": {"tier": "free-tier", "plan": "Antigravity starter quota"},
                "authType": "oauth",
            }
        )
        assert is_free
        assert "tier" in rule

    @pytest.mark.parametrize(
        ("connection", "expected_rule"),
        [
            (
                {"providerSpecificData": {"tier": "free user@secret.example.com"}},
                "providerSpecificData.tier",
            ),
            (
                {"providerSpecificData": {"plan": "free user@secret.example.com"}},
                "providerSpecificData.plan",
            ),
            ({"plan_label": "free user@secret.example.com"}, "plan_label"),
        ],
    )
    def test_free_signal_evidence_never_contains_source_value(
        self, connection: dict[str, object], expected_rule: str
    ) -> None:
        is_free, rule = connection_signals_free(connection)
        assert is_free
        assert rule == expected_rule
        assert "user@secret.example.com" not in rule

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

    def test_capacity_order_unchanged_in_story_1(self) -> None:
        # Story 3 changes this order (free-first). Story 1 must not.
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


# Kilocode (and kc alias) :free tails that origin/main classed SUBSCRIPTION
# because oauth won over the suffix.  Each tail exists under both prefixes.
_KILOCODE_FREE_TAILS: tuple[str, ...] = (
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "dots-studio/dots-3-note-preview:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "qwen/qwen3.8-27b:free",
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "thinkingmachines/inkling-small:free",
    "poolside/laguna-xs-2.1:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "stepfun/step-3.7-flash:free",
    "deepseek-v4-flash-0731:free",
    "dots-3-note-preview:free",
    "glm-5.2:free",
    "hy3:free",
    "inkling-small:free",
    "kat-coder-pro-v2.5:free",
    "laguna-m.1:free",
    "laguna-xs-2.1:free",
    "lfm-2.5-2.6b:free",
    "ling-3.0-flash-fin:free",
    "ling-3.0-flash-sante:free",
    "ling-3.0-flash-vl:free",
    "nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nemotron-3-super-120b-a12b:free",
    "nemotron-3-ultra-550b-a55b:free",
    "nemotron-3.5-content-safety:free",
    "nemotron-3.5-lightning:free",
    "nex-n2.5-mini:free",
    "nex-n2.5-pro:free",
    "north-mini-code:free",
    "qwen3.8-27b:free",
    "step-3.7-flash:free",
)


def _expected_class_changes() -> dict[str, tuple[CapacityClass, CapacityClass, str]]:
    """Every route whose class differs from origin/main, with the new rule."""
    out: dict[str, tuple[CapacityClass, CapacityClass, str]] = {}
    for tail in _KILOCODE_FREE_TAILS:
        for prefix in ("kc", "kilocode"):
            out[f"{prefix}/{tail}"] = (
                CapacityClass.SUBSCRIPTION,
                CapacityClass.FREE,
                "free_suffix",
            )
    out["cmd/inclusionai/ling-3.0-flash-sante:free"] = (
        CapacityClass.UNKNOWN,
        CapacityClass.FREE,
        "free_suffix",
    )
    out["command-code/inclusionai/ling-3.0-flash-sante:free"] = (
        CapacityClass.UNKNOWN,
        CapacityClass.FREE,
        "free_suffix",
    )
    return out


class _OriginMainCapacityLadder(EligibilityLadder):
    """Same ladder, origin/main ``_capacity_class`` rules (no :free suffix)."""

    def _capacity_class(
        self, conn: Mapping[str, Any] | None, row: Mapping[str, Any], route_id: str = ""
    ) -> tuple[CapacityClass, str, str]:
        if conn is None:
            return CapacityClass.UNKNOWN, "", "no_connection"
        plan_label = str(conn.get("plan_label", ""))
        plan_lower = plan_label.lower()
        auth_type = str(conn.get("authType", "")).lower()
        pricing = row.get("pricing")
        prices: list[float] = []
        if isinstance(pricing, Mapping):
            for value in pricing.values():
                if isinstance(value, (int, float)):
                    prices.append(float(value))
        all_zero = bool(prices) and all(p == 0 for p in prices)
        positive = any(p > 0 for p in prices)
        if auth_type == "oauth" and "free" not in plan_lower:
            return CapacityClass.SUBSCRIPTION, plan_label, "oauth_subscription"
        if bool(conn.get("import_free_only")) or "free" in plan_lower or all_zero:
            return CapacityClass.FREE, plan_label, "legacy_free"
        if auth_type == "apikey" and positive:
            return CapacityClass.METERED, plan_label, "apikey_positive_pricing"
        return CapacityClass.UNKNOWN, plan_label, "unknown"


class TestRankingParityAgainstOriginMain:
    """Selection order is unchanged for every route whose class did not change."""

    def test_ranking_inventory_is_sanitized_and_capped(self) -> None:
        path = FIXTURES / "ranking_inventory.json"
        raw = path.read_bytes()
        assert len(raw) < 1_000_000
        text = raw.decode("utf-8").lower()
        for needle in ("/home/", "gmail.com", "bearer ", "copilottoken", "password"):
            assert needle not in text

    def test_ranked_order_unchanged_except_listed_class_changes(self, tmp_path: Path) -> None:
        inventory = json.loads((FIXTURES / "ranking_inventory.json").read_text())
        connections = json.loads((FIXTURES / "ranking_connections.json").read_text())
        req = TaskRequirements(required_capabilities=frozenset({"tools"}))

        def probe(route_id: str) -> HealthResult:
            return HealthResult(healthy=True, category="", status_code=200)

        old = _OriginMainCapacityLadder(
            inventory_rows=inventory,
            connections=connections,
            probe=probe,
            state_path=tmp_path / "old-state.json",
        )
        new = EligibilityLadder(
            inventory_rows=inventory,
            connections=connections,
            probe=probe,
            state_path=tmp_path / "new-state.json",
        )
        old_verdicts = {v.route_id: v for v in old.evaluate(req, now=NOW)}
        new_verdicts = {v.route_id: v for v in new.evaluate(req, now=NOW)}
        assert set(old_verdicts) == set(new_verdicts)

        expected = _expected_class_changes()
        observed: dict[str, tuple[CapacityClass, CapacityClass]] = {}
        for rid, nv in new_verdicts.items():
            ov = old_verdicts[rid]
            if ov.capacity_class != nv.capacity_class:
                observed[rid] = (ov.capacity_class, nv.capacity_class)

        assert set(observed) == set(expected), (
            f"unexpected class changes: extra={sorted(set(observed) - set(expected))[:8]}"
            f" missing={sorted(set(expected) - set(observed))[:8]}"
        )
        for rid, (old_c, new_c, rule) in expected.items():
            assert observed[rid] == (old_c, new_c), rid
            _cap, _plan, evidence = new._capacity_class(
                new._connection_for(new_verdicts[rid].provider), new._rows[rid], route_id=rid
            )
            assert evidence == rule, f"{rid} evidence {evidence!r} != {rule!r}"

        # Relative ranked order of routes whose class did not change is identical.
        def ranked(verdicts: dict[str, Any]) -> list[str]:
            kept = [
                v for v in verdicts.values() if v.rank is not None and v.route_id not in expected
            ]
            kept.sort(key=lambda v: v.rank or 0)
            return [v.route_id for v in kept]

        assert ranked(old_verdicts) == ranked(new_verdicts)

    def test_agy_exclude_routes_excludes_antigravity_pool(self) -> None:
        ladder = _make_ladder()
        req = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            exclude_routes=frozenset({"agy/claude-sonnet-4-6"}),
        )
        verdicts = {v.route_id: v for v in ladder.evaluate(req, now=NOW)}
        assert verdicts["agy/claude-sonnet-4-6"].reason == "excluded_route"
        assert verdicts["antigravity/claude-sonnet-4-6"].reason == "excluded_family"
        assert verdicts["antigravity/gpt-oss-120b-medium"].reason == "excluded_family"


# -----------------------------------------------------------------------
# F. CATALOG-GHOST COOLDOWN WIRING (_record_health end-to-end)
# -----------------------------------------------------------------------


def _make_minimal_ladder(state_path: Path) -> EligibilityLadder:
    """Build an EligibilityLadder with no inventory or connections for unit testing."""
    return EligibilityLadder(
        inventory_rows=[],
        connections=[],
        probe=lambda _route: HealthResult(True, "healthy"),
        state_path=state_path,
    )


_CATALOG_MSG = "Model 'x' is not available in the active live catalog for provider 'y'"
_UNRELATED_MSG = "Your request was rejected due to content policy."


class TestCatalogGhostWiring:
    def test_health_result_truncates_long_error_message(self) -> None:
        """HealthResult.__post_init__ caps error_message at 500 chars unconditionally."""
        long_msg = "x" * 5000
        result = HealthResult(healthy=False, category="unsupported", error_message=long_msg)
        assert len(result.error_message) <= 500

    def test_catalog_stale_message_produces_catalog_stale_cooldown(self, tmp_path: Path) -> None:
        """_record_health with a catalog-stale error message → category catalog_stale."""
        ladder = _make_minimal_ladder(tmp_path / "state.json")
        result = HealthResult(
            healthy=False, category="unsupported", status_code=400, error_message=_CATALOG_MSG
        )
        ladder._record_health("cx/some-model", result, NOW)
        health = ladder._state["health"]["cx/some-model"]
        assert health["category"] == "catalog_stale", health
        cooldown = ladder._state["cooldowns"].get("route:cx/some-model")
        assert cooldown is not None
        assert cooldown["category"] == "catalog_stale"
        until_dt = datetime.fromisoformat(cooldown["until"])
        expected_min = NOW + timedelta(seconds=CATALOG_STALE_COOLDOWN_SECONDS - 5)
        assert until_dt >= expected_min, until_dt

    def test_unrelated_400_stays_unsupported(self, tmp_path: Path) -> None:
        """_record_health with an unrelated 400 message → category stays unsupported."""
        ladder = _make_minimal_ladder(tmp_path / "state.json")
        result = HealthResult(
            healthy=False, category="unsupported", status_code=400, error_message=_UNRELATED_MSG
        )
        ladder._record_health("cx/some-model", result, NOW)
        health = ladder._state["health"]["cx/some-model"]
        assert health["category"] == "unsupported", health

    def test_e2e_http_response_to_persisted_state(self, tmp_path: Path) -> None:
        """True end-to-end: fake HTTP 400 → classify_probe_status → _record_health →
        save to disk → reload → assert catalog_stale category, correct cooldown window,
        and raw error text absent from the saved file.
        """
        from verdict.subagent_selection import classify_probe_status

        state_path = tmp_path / "state.json"
        ladder = _make_minimal_ladder(state_path)

        # Simulate the response the probe receives from OmniRoute.
        http_body = "Model 'x' is not available in the active live catalog for provider 'y'"
        health_result = classify_probe_status(400, body=http_body)
        assert health_result.category in ("unsupported", "unservable", "bad_request"), (
            f"classifier changed: {health_result.category}"
        )

        ladder._record_health("cx/ghost-model", health_result, NOW)

        # --- in-memory checks ---
        assert ladder._state["health"]["cx/ghost-model"]["category"] == "catalog_stale"
        cooldown = ladder._state["cooldowns"]["route:cx/ghost-model"]
        until_dt = datetime.fromisoformat(cooldown["until"])
        expected_min = NOW + timedelta(seconds=CATALOG_STALE_COOLDOWN_SECONDS - 5)
        expected_max = NOW + timedelta(seconds=CATALOG_STALE_COOLDOWN_SECONDS + 5)
        assert until_dt >= expected_min, f"cooldown too short: {until_dt}"
        assert until_dt <= expected_max, f"cooldown too long: {until_dt}"

        # --- reload from disk ---
        assert state_path.exists(), "state file was not persisted"
        raw_text = state_path.read_text(encoding="utf-8")
        reloaded = json.loads(raw_text)
        assert reloaded["health"]["cx/ghost-model"]["category"] == "catalog_stale"
        reloaded_until = datetime.fromisoformat(
            reloaded["cooldowns"]["route:cx/ghost-model"]["until"]
        )
        assert reloaded_until >= expected_min
        assert reloaded_until <= expected_max

        # error text must NOT appear anywhere in the saved file
        assert "not available in the active live catalog" not in raw_text, (
            "raw error text was persisted"
        )
        assert "error_message" not in raw_text, "error_message field was persisted"

    def test_is_catalog_stale_rejects_category_strings(self) -> None:
        """Negative predicate coverage: category strings do not trigger catalog_stale.

        This guards the old wiring bug where ``category`` (e.g. "unsupported")
        was passed instead of ``error_message``.  Those strings never contain the
        catalog-stale phrase, so ``is_catalog_stale_error`` returns False for them.
        """
        from verdict.orchestration.provider_catalog import is_catalog_stale_error

        assert not is_catalog_stale_error("unsupported")
        assert not is_catalog_stale_error("bad_request")
        assert not is_catalog_stale_error("unservable")
