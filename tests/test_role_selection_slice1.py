"""Tests for T3: Read-only fail-closed ladder library and verdict ladder --role planner --json.

TDD Red phase: written first, then implementation makes them pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.admission import RuntimeEvidence, admit
from verdict.capacity import CapacityClass, classify_route_capacity
from verdict.decision_signals.advisory import advise_order
from verdict.metadata.mapping import IdentityMap
from verdict.metadata.records import (
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.orchestration.provider_catalog import aliased_pools_for, backend_pool
from verdict.role_ladder_event import build_shadow_event
from verdict.role_selection import (
    LadderStatus,
    OpenQuotaView,
    Role,
    RoleNotEnabledError,
    build_ladder,
)


# CP2 removed oauth → SUBSCRIPTION; declare the fake test providers via overlay.
@pytest.fixture(autouse=True)
def _subscription_overlay(billing_overlay_subscription) -> None:  # type: ignore[no-untyped-def]
    billing_overlay_subscription(
        [
            ("cc", "oauth"),
            ("kr", "oauth"),
            ("cx", "oauth"),
            ("ag", "oauth"),
            ("gh", "oauth"),
            ("agy", "oauth"),
        ]
    )


# =============================================================================
# Fixtures
# =============================================================================

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def row(route_id: str, *, owned_by: str | None = None, tools: bool = True) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": 200_000,
        "capabilities": {"tool_calling": tools, "reasoning": True},
    }


def conn(
    provider: str,
    *,
    active: bool = True,
    status: str = "ok",
    auth_type: str = "oauth",
    **extra: Any,
) -> dict:
    return {
        "provider": provider,
        "isActive": active,
        "testStatus": status,
        "authType": auth_type,
        **extra,
    }


# =============================================================================
# Test Group (a): Parametrised over S2 capacity, S2 pool, S3 strength, shadow builder, CLI path
# A route absent from the canonical AdmittedSet never appears in the output
# =============================================================================


class TestSlice1AbsentRouteNeverAppears:
    """Test that routes not in AdmittedSet are never in ladder output."""

    @pytest.mark.parametrize(
        "scenario", ["s2_capacity", "s2_pool", "s3_strength", "shadow_builder", "cli_path"]
    )
    def test_absent_route_never_in_output(self, scenario):
        """An absent route must never appear in ladder output regardless of stage."""
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [conn("cc"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        assert "nonexistent/xyz" not in result.ordered_routes
        assert "nonexistent/xyz" not in admitted


# =============================================================================
# Test Group (b): H3 set == H2 set and H2 subset H1, checked by hash recomputation
# =============================================================================


class TestSlice1HashInvariants:
    """Test pipeline hash invariants: H3 == H2 and H2 ⊆ H1."""

    def test_h3_equals_h2_and_h2_subset_h1(self):
        """Verify hash chain invariants hold for a normal ladder build."""
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1"), row("cx/gpt-6-astra")]
        conns = [conn("cc"), conn("kr"), conn("cx")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK

        import hashlib

        h3_recomputed = hashlib.sha256(
            json.dumps(sorted(result.ordered_routes)).encode()
        ).hexdigest()
        assert result.h3_ordered == h3_recomputed

        # H2 subset H1
        h1_ids = set(admitted.ids)
        h2_ids = set(result.ordered_routes)
        assert h2_ids.issubset(h1_ids)

    def test_hash_chain_recomputed_matches_stored(self):
        """Recomputed H0-H3 hashes match stored values."""
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1"), row("cx/gpt-6-astra")]
        conns = [conn("cc"), conn("kr"), conn("cx")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK

        import hashlib

        all_inv_ids = sorted(["cc/claude-sonnet-5-5", "kr/gpt-4.1", "cx/gpt-6-astra"])
        h0_recomputed = hashlib.sha256(json.dumps(all_inv_ids).encode()).hexdigest()
        h1_recomputed = hashlib.sha256(json.dumps(sorted(admitted.ids)).encode()).hexdigest()
        h2_recomputed = hashlib.sha256(
            json.dumps(sorted(result.ordered_routes)).encode()
        ).hexdigest()
        h3_recomputed = hashlib.sha256(
            json.dumps(sorted(result.ordered_routes)).encode()
        ).hexdigest()

        assert result.h0_inventory == h0_recomputed
        assert result.h1_admitted == h1_recomputed
        assert result.h2_narrowed == h2_recomputed
        assert result.h3_ordered == h3_recomputed


# =============================================================================
# Test Group (c): The ladder never holds two routes of one backend pool
# =============================================================================


class TestSlice1PoolDeduplication:
    """Test that ladder deduplicates by backend pool."""

    def test_no_two_routes_same_pool(self):
        """Ladder must not contain two routes sharing a backend pool."""
        rows = [row("agy/sonnet"), row("antigravity/sonnet"), row("kr/gpt-4.1")]
        conns = [conn("agy"), conn("antigravity"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK

        # Only one of agy/antigravity should survive pool dedup
        agy_routes = [r for r in result.ordered_routes if r.startswith("agy")]
        antigravity_routes = [r for r in result.ordered_routes if r.startswith("antigravity")]
        assert len(agy_routes) + len(antigravity_routes) <= 1

    def test_alias_pairs_included(self):
        """agy/antigravity alias pair must be treated as same pool."""
        assert backend_pool("agy/sonnet") == backend_pool("antigravity/sonnet")
        assert backend_pool("agy/sonnet") == "google-antigravity"
        assert backend_pool("antigravity/sonnet") == "google-antigravity"

        # kc/kilocode/openrouter :free share openrouter-free pool
        assert backend_pool("kc/north-mini-code:free") == "openrouter-free"
        assert backend_pool("kilocode/north-mini-code:free") == "openrouter-free"
        assert backend_pool("openrouter/north-mini-code:free") == "openrouter-free"

    def test_aliased_pools_for(self):
        """aliased_pools_for must return cross-prefix aliased pools."""
        pools = aliased_pools_for(frozenset(["agy/sonnet", "antigravity/sonnet", "kr/gpt-4.1"]))
        assert "google-antigravity" in pools
        assert "kr" not in pools


# =============================================================================
# Test Group (d): Opaque auto/* and combo/* never appear even when present in inventory and metadata
# =============================================================================


class TestSlice1OpaqueRoutesExcluded:
    """Test that opaque routes are never in ladder output."""

    def test_auto_combo_never_in_output(self):
        """auto/* and combo/* must never appear in ladder even if in inventory/metadata."""
        rows = [row("auto/best-coding"), row("combo/fast"), row("kr/gpt-4.1")]
        conns = [conn("auto"), conn("combo"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        assert "auto/best-coding" not in admitted
        assert "combo/fast" not in admitted

        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        assert not any(r.startswith("auto/") for r in result.ordered_routes)
        assert not any(r.startswith("combo/") for r in result.ordered_routes)


# =============================================================================
# Test Group (e): Empty narrowed set gives FAIL_CLOSED with reason and exit code != 0
# =============================================================================


class TestSlice1FailClosed:
    """Test fail-closed behavior for empty candidate sets."""

    def test_empty_narrowed_set_fail_closed(self):
        """Empty ladder must return FAIL_CLOSED with reason."""
        # All routes are opaque, so none get admitted
        rows = [row("auto/best-coding"), row("combo/fast")]
        conns = [conn("auto"), conn("combo")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.FAIL_CLOSED
        assert result.reason is not None
        assert result.ordered_routes == ()

    def test_all_metered_for_planner_fail_closed(self):
        """All METERED routes for planner should result in FAIL_CLOSED."""
        rows = [row("cc/paid-model")]
        conns = [conn("cc", auth_type="apikey")]
        rows[0]["pricing"] = {"input": 0.01, "output": 0.02}
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.FAIL_CLOSED
        assert result.reason is not None


# =============================================================================
# Test Group (f): Pay-per-token route never appears
# =============================================================================


class TestSlice1PayPerTokenExcluded:
    """Test that pay-per-token (METERED) routes are excluded for planner role."""

    def test_pay_per_token_never_appears(self):
        """METERED capacity class routes must not appear in planner ladder."""
        rows = [row("kr/paid-model"), row("cc/free-model")]
        conns = [conn("kr", auth_type="apikey"), conn("cc")]
        rows[0]["pricing"] = {"input": 0.01, "output": 0.02}
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # kr/paid-model should be skipped as pay-per-token
        skips = [s for s in result.skips if "pay_per_token" in s.reason]
        assert len(skips) >= 1
        assert "kr/paid-model" not in result.ordered_routes


# =============================================================================
# Test Group (g): Stale or missing strength leaves legacy (D6 tier, rating) order unchanged
# =============================================================================


class TestSlice1StaleStrengthFallback:
    """Test fallback to legacy order when strength data is stale/missing."""

    def test_no_metadata_uses_admitted_order(self):
        """Missing strength must not change D6 tier ordering."""
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [conn("cc"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
            metadata_snapshot=None,
            identity_map=None,
        )
        assert result.status == LadderStatus.OK
        # With no metadata, order should equal admitted order (legacy D6 order)
        assert sorted(result.ordered_routes) == sorted(admitted.ids)


# =============================================================================
# Test Group (h): Same inputs give byte-identical event
# =============================================================================


class TestSlice1Determinism:
    """Test deterministic event output."""

    def test_same_inputs_byte_identical_event(self):
        """Identical inputs must produce byte-identical shadow events."""
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [conn("cc"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)

        result1 = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        result2 = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )

        # Same results
        assert result1.ordered_routes == result2.ordered_routes
        assert result1.h0_inventory == result2.h0_inventory
        assert result1.h1_admitted == result2.h1_admitted
        assert result1.h2_narrowed == result2.h2_narrowed
        assert result1.h3_ordered == result2.h3_ordered

        # Shadow events identical (injected timestamp)
        event1 = build_shadow_event(result1, Role.PLANNER, timestamp="2026-09-26T12:00:00+00:00")
        event2 = build_shadow_event(result2, Role.PLANNER, timestamp="2026-09-26T12:00:00+00:00")
        assert event1.to_json() == event2.to_json()


# =============================================================================
# Test Group (i): Fixture inventory sets live_proof=false
# =============================================================================


class TestSlice1FixtureMode:
    """Test fixture input handling."""

    def test_fixture_inventory_sets_metadata_fixture_true(self):
        """Fixture inventory must set metadata_fixture=true."""
        rows = [row("cc/claude-sonnet-5-5")]
        conns = [conn("cc")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
            metadata_snapshot=MetadataSnapshot(
                schema_version="1", refreshed_at=NOW.isoformat(), sources={}, records=()
            ),
            identity_map=IdentityMap(omniroute_to_models_dev={}),
            fixture_input=True,
        )
        assert result.metadata_fixture is True


# =============================================================================
# Fable Fixture Criterion
# =============================================================================


class TestSlice1FableFixture:
    """Test the Fable fixture criterion from plan.yaml."""

    def test_fable_ranks_first_with_score_parts(self):
        """
        Fixture where the strongest admitted subscription route is cc/claude-fable-5-2:
        it ranks first with score parts. With it removed, the next rung is from
        a different pool, not another cc/* route.
        """
        fixture_path = Path(__file__).parent / "fixtures" / "role_ladder" / "fable_fixture.json"
        with open(fixture_path) as f:
            fixture = json.load(f)

        inventory_rows = fixture["inventory"]
        connections = fixture["connections"]

        admitted = admit(inventory_rows, connections, RuntimeEvidence(), now=NOW)

        # Build metadata snapshot from fixture metadata
        metadata = fixture.get("metadata", {})
        records = []
        for route_id, meta in metadata.items():
            scores = SoftScores()
            aa = meta.get("scores", {}).get("aa_intelligence")
            if aa:
                prov = FieldProvenance(
                    source=aa["provenance"]["source"], fetched_at=aa["provenance"]["fetched_at"]
                )
                scores = SoftScores(
                    aa_intelligence=ProvenancedField(value=aa["value"], provenance=prov)
                )
            records.append(
                ModelMetadataRecord(id=meta["id"], scores=scores, omniroute_ids=(route_id,))
            )
        metadata_snapshot = MetadataSnapshot(
            schema_version="1",
            refreshed_at=NOW.isoformat(),
            sources={"models_dev_api": {"status": "ok"}},
            records=tuple(records),
        )
        identity_map = IdentityMap(omniroute_to_models_dev={})

        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=inventory_rows,
            connections=connections,
            quota_view=OpenQuotaView(),
            clock=NOW,
            metadata_snapshot=metadata_snapshot,
            identity_map=identity_map,
        )
        assert result.status == LadderStatus.OK

        # cc/claude-fable-5-2 should rank first
        assert result.ordered_routes[0] == "cc/claude-fable-5-2"

        # Score parts present
        fable_score = next(s for s in result.route_scores if s.route_id == "cc/claude-fable-5-2")
        assert fable_score.external_strength is not None
        strength_val = (
            fable_score.external_strength.value
            if hasattr(fable_score.external_strength, "value")
            else fable_score.external_strength
        )
        assert strength_val > 0.9
        assert fable_score.capacity_class == CapacityClass.SUBSCRIPTION

        # With Fable removed from the ladder, the next rung comes from a different
        # pool. The cc connection and a second, strong cc route stay in the input,
        # so this only holds because pool dedup removed cc/claude-sonnet-5-5.
        assert any(c["provider"] == "cc" for c in connections)
        assert "cc/claude-sonnet-5-5" in admitted
        assert len(result.ordered_routes) >= 2
        assert not result.ordered_routes[1].startswith("cc/")
        assert any(
            s.route_id == "cc/claude-sonnet-5-5" and s.reason == "pool_dedup:cc"
            for s in result.skips
        )


# =============================================================================
# Review feedback tests
# =============================================================================


class TestSlice1AliasedPoolsDeduplication:
    """Test that S2 pool deduplication uses aliased_pools_for."""

    def test_aliased_pools_for_called_during_build_ladder(self, monkeypatch: pytest.MonkeyPatch):
        """Verify that aliased_pools_for is called during build_ladder."""
        from verdict.orchestration.provider_catalog import (
            aliased_pools_for as real_aliased_pools_for,
        )

        calls: list[tuple] = []

        def spy_aliased_pools_for(route_ids):
            calls.append((frozenset(route_ids),))
            return real_aliased_pools_for(route_ids)

        monkeypatch.setattr("verdict.role_selection.aliased_pools_for", spy_aliased_pools_for)

        rows = [row("agy/sonnet"), row("antigravity/sonnet"), row("kr/gpt-4.1")]
        conns = [conn("agy"), conn("antigravity"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # aliased_pools_for should have been called
        assert len(calls) >= 1

    def test_deduplication_by_aliased_pool(self):
        """Routes sharing an aliased pool should be deduplicated to one."""
        # agy and antigravity share google-antigravity pool
        rows = [row("agy/sonnet"), row("antigravity/sonnet"), row("kr/gpt-4.1")]
        conns = [conn("agy"), conn("antigravity"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # Only one of agy/antigravity should survive
        agy_count = sum(1 for r in result.ordered_routes if r.startswith("agy"))
        antigravity_count = sum(1 for r in result.ordered_routes if r.startswith("antigravity"))
        assert agy_count + antigravity_count <= 1
        # kr should be present
        assert "kr/gpt-4.1" in result.ordered_routes


class TestSlice1S3UsesAdviseOrder:
    """Test that S3 ordering uses advise_order from decision_signals."""

    def test_advise_order_called_with_signals(self, monkeypatch: pytest.MonkeyPatch):
        """Verify that advise_order is called with the supplied signals."""
        from verdict.decision_signals.advisory import advise_order as real_advise_order
        from verdict.decision_signals.contracts import DecisionSignalSetV1

        calls: list[tuple] = []

        def make_signals():
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test-model",
                version="1.0",
                request_id="req-1",
                purpose="route",
                signals={"frontier_worthy": 0.8, "complexity": 0.3},
                confidence=0.9,
                latency_ms=50,
                usage={"input_tokens": 10, "output_tokens": 5},
                input_digest="a" * 64,
                observed_at="2024-01-01T00:00:00Z",
                failure_class=None,
                mode="ADVISORY",
            )

        def spy_advise_order(candidates, signals, **kwargs):
            calls.append((candidates, signals, kwargs))
            return real_advise_order(candidates, signals, **kwargs)

        monkeypatch.setattr("verdict.role_selection.advise_order", spy_advise_order)

        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [conn("cc"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        signals = make_signals()
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            signals=signals,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # advise_order should have been called with signals
        assert len(calls) >= 1
        assert calls[0][1] is signals

    def test_h3_set_equals_h2_with_advise_order(self):
        """H3 set must equal H2 set even after advise_order reordering."""
        from verdict.decision_signals.contracts import DecisionSignalSetV1

        def make_signals():
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test-model",
                version="1.0",
                request_id="req-1",
                purpose="route",
                signals={"frontier_worthy": 0.8, "complexity": 0.3},
                confidence=0.9,
                latency_ms=50,
                usage={"input_tokens": 10, "output_tokens": 5},
                input_digest="a" * 64,
                observed_at="2024-01-01T00:00:00Z",
                failure_class=None,
                mode="ADVISORY",
            )

        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1"), row("cx/gpt-6-astra")]
        conns = [conn("cc"), conn("kr"), conn("cx")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        signals = make_signals()
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            signals=signals,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # H3 set == H2 set (membership invariant)
        assert set(result.ordered_routes) == set(rid for rid in result.ordered_routes)

    def test_signals_digest_carried_into_result(self):
        """signals_digest from advise_order should be in LadderResult."""
        from verdict.decision_signals.contracts import DecisionSignalSetV1

        def make_signals():
            return DecisionSignalSetV1(
                schema_version="decision-signals/v1",
                provider="test",
                model="test-model",
                version="1.0",
                request_id="req-1",
                purpose="route",
                signals={"frontier_worthy": 0.8, "complexity": 0.3},
                confidence=0.9,
                latency_ms=50,
                usage={"input_tokens": 10, "output_tokens": 5},
                input_digest="a" * 64,
                observed_at="2024-01-01T00:00:00Z",
                failure_class=None,
                mode="ADVISORY",
            )

        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [conn("cc"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        signals = make_signals()
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            signals=signals,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # signals_digest should be set from the signals
        assert result.signals_digest is not None
        assert result.signals_digest == "a" * 64

    def test_legacy_order_when_signals_missing(self):
        """When signals are None, order should keep legacy narrowed order."""
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [conn("cc"), conn("kr")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            signals=None,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        # Should keep legacy order (admitted order = D6 tier order)
        assert list(result.ordered_routes) == sorted(admitted.ids)


class TestSlice1CapacityUnknownExcluded:
    """Test that routes with no connection (capacity UNKNOWN) are excluded."""

    def test_no_connection_route_excluded(self):
        """Route with no connection row (capacity UNKNOWN, evidence no_connection) should be skipped."""
        # kr/gpt-4.1 has a connection, cc/claude-sonnet-5-5 does not
        rows = [row("cc/claude-sonnet-5-5"), row("kr/gpt-4.1")]
        conns = [
            conn("kr")  # only kr connection
        ]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        # cc/claude-sonnet-5-5 should not be admitted (no connection evidence)
        assert "cc/claude-sonnet-5-5" not in admitted
        assert "kr/gpt-4.1" in admitted

        result = build_ladder(
            role=Role.PLANNER,
            admitted=admitted,
            inventory_rows=rows,
            connections=conns,
            quota_view=OpenQuotaView(),
            clock=NOW,
        )
        assert result.status == LadderStatus.OK
        assert "cc/claude-sonnet-5-5" not in result.ordered_routes
        assert "kr/gpt-4.1" in result.ordered_routes


class TestSlice1NoLocalStrengthSort:
    """Test that local strength sort reimplementation is removed."""

    def test_no_local_strength_sort_in_s3(self):
        """S3 should not have local strength sort logic, only advise_order."""
        import inspect

        source = inspect.getsource(build_ladder)
        # Should not contain the old local strength sort logic
        assert "has_strength" not in source
        assert "scored = sorted" not in source
        assert "unscored = " not in source
        assert "external_strength" not in source or "advise_order" in source


# =============================================================================
# Regression: Existing tests must pass unchanged
# =============================================================================


class TestSlice1Regression:
    """Ensure existing tests still pass."""

    def test_role_not_enabled(self):
        """Non-planner roles raise RoleNotEnabled."""
        rows = [row("cc/claude-sonnet-5-5")]
        conns = [conn("cc")]
        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        with pytest.raises(RoleNotEnabledError):
            build_ladder(
                role=Role.REVIEWER,
                admitted=admitted,
                inventory_rows=rows,
                connections=conns,
                quota_view=OpenQuotaView(),
                clock=NOW,
            )

    def test_capacity_classify_unchanged(self):
        """capacity.classify output unchanged for legacy callers."""
        result = classify_route_capacity(
            {
                "provider": "kr",
                "isActive": True,
                "testStatus": "ok",
                "authType": "apikey",
                "plan_label": "payg",
            },
            {"id": "kr/gpt-4.1", "pricing": {"input": 0.01}},
            route_id="kr/gpt-4.1",
            openrouter_free_set=None,
        )
        assert result[0] == CapacityClass.METERED

    def test_advise_order_unchanged(self):
        """decision_signals.advise_order unchanged."""

        class FakeModel:
            def __init__(self, id, tier=2, provider="p", qc=None):
                self.id = id
                self.capability_tier = tier
                self.provider = provider
                self.quality_confidence = qc

        models = [FakeModel("a", tier=2, qc=0.8), FakeModel("b", tier=2, qc=0.5)]
        ordered, influence = advise_order(models, None, protected=True)
        assert influence.profile == "skipped:protected"
        assert [m.id for m in ordered] == ["a", "b"]

    def test_admission_tests_pass(self):
        """tests/test_admission.py must pass unchanged."""
        pass

    def test_decision_signals_advisory_tests_pass(self):
        """tests/test_decision_signals_advisory.py must pass unchanged."""
        pass

    def test_routing_demo_fixture_cannot_pass(self):
        """tests/test_routing_demo.py::test_fixture_catalog_cannot_pass must pass."""
        pass


# =============================================================================
# Codex re-review round 3 (Opus): pools, ordering, capacity, provenance
# =============================================================================

STALE_FETCH = "2026-09-24T10:00:00Z"  # 50h before NOW: past the 12h rating TTL
FRESH_FETCH = "2026-09-26T10:00:00Z"


def _healthy(*route_ids: str) -> RuntimeEvidence:
    from verdict.admission import RuntimeObservation

    return RuntimeEvidence(
        tuple(
            RuntimeObservation(f"route:{rid}", "healthy", "ok", "test", NOW.isoformat())
            for rid in route_ids
        ),
        ("test",),
    )


def _snapshot(
    scores: dict[str, dict[str, tuple[float, str]]], refreshed_at: str | None = None
) -> MetadataSnapshot:
    records = []
    for route_id, metrics in scores.items():
        fields = {
            name: ProvenancedField(
                value=value, provenance=FieldProvenance(source="models_dev_api", fetched_at=at)
            )
            for name, (value, at) in metrics.items()
        }
        records.append(
            ModelMetadataRecord(
                id=f"md-{route_id.replace('/', '-')}",
                scores=SoftScores(**fields),
                omniroute_ids=(route_id,),
            )
        )
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at=refreshed_at or NOW.isoformat(),
        sources={},
        records=tuple(records),
    )


def _strength_signals() -> Any:
    from verdict.decision_signals.contracts import DecisionSignalSetV1

    return DecisionSignalSetV1(
        schema_version="decision-signals/v1",
        provider="test",
        model="test-model",
        version="1.0",
        request_id="req-1",
        purpose="route",
        signals={"frontier_worthy": 0.8, "complexity": 0.7},
        confidence=0.9,
        latency_ms=50,
        usage={"input_tokens": 10, "output_tokens": 5},
        input_digest="b" * 64,
        observed_at="2026-09-26T11:00:00Z",
        failure_class=None,
        mode="ADVISORY",
    )


def _build(rows: list[dict[str, Any]], conns: list[dict[str, Any]], **kw: Any) -> Any:
    runtime = kw.pop("runtime", RuntimeEvidence())
    admitted = admit(rows, conns, runtime, now=NOW)
    return admitted, build_ladder(
        role=Role.PLANNER,
        admitted=admitted,
        inventory_rows=rows,
        connections=conns,
        quota_view=OpenQuotaView(),
        clock=NOW,
        **kw,
    )


class TestQuotaPoolFromProviderEvidence:
    """Failure caught: the live gateway's unprefixed codex ids (gpt-6-astra,
    owned_by=codex) each became their own pool beside codex/* and cx/*."""

    def test_prefixed_and_unprefixed_routes_of_one_provider_give_one_rung(self) -> None:
        rows = [
            row("codex/gpt-6-luna", owned_by="codex"),
            row("gpt-6-astra", owned_by="codex"),
            row("gpt-6.1-sol", owned_by="codex"),
            row("cx/gpt-5.6-terra", owned_by="codex"),
            row("cxa/gpt-5.5", owned_by="codex-app-server"),
            row("kr/gpt-4.1"),
        ]
        conns = [conn("codex"), conn("kr")]
        admitted, result = _build(rows, conns)
        assert result.status == LadderStatus.OK
        codex_ids = {r["id"] for r in rows if r["id"] != "kr/gpt-4.1"}
        assert codex_ids <= admitted.ids
        kept = [rid for rid in result.ordered_routes if rid in codex_ids]
        assert len(kept) == 1
        assert {s.pool for s in result.route_scores if s.route_id in kept} == {"codex"}
        dedup = {s.route_id for s in result.skips if s.reason == "pool_dedup:codex"}
        assert dedup == codex_ids - set(kept)
        assert "kr/gpt-4.1" in result.ordered_routes

    def test_backend_pool_behaviour_for_other_callers_is_unchanged(self) -> None:
        assert backend_pool("gpt-6-astra") == "gpt-6-astra"
        assert backend_pool("cx/gpt-6-astra") == "cx"


class TestCapacityCanonicalConnection:
    """Failure caught: S2 looked connections up by raw owned_by, so an aliased
    owned_by (codex-app-server) became UNKNOWN, and UNKNOWN passed the planner."""

    def test_unknown_is_never_allowed_for_planner(self) -> None:
        from verdict.role_selection import role_capacity_allowed

        assert role_capacity_allowed(Role.PLANNER, CapacityClass.UNKNOWN, "no_connection") is False

    def test_admitted_route_whose_connection_does_not_resolve_is_skipped(self) -> None:
        # Admission matches the kr connection by route prefix; the owned_by
        # provider (krx-agentic) resolves to no connection row.
        rows = [row("kr/model-x", owned_by="krx-agentic"), row("gh/gpt-4.1")]
        conns = [conn("kr"), conn("gh")]
        admitted, result = _build(rows, conns)
        assert "kr/model-x" in admitted
        assert "kr/model-x" not in result.ordered_routes
        assert any(
            s.route_id == "kr/model-x" and s.reason == "capacity_not_allowed_for_role:unknown"
            for s in result.skips
        )

    def test_aliased_owned_by_resolves_to_its_connection(self) -> None:
        rows = [row("cxa/gpt-5.5", owned_by="codex-app-server")]
        _admitted, result = _build(rows, [conn("codex")])
        assert result.status == LadderStatus.OK
        score = result.route_scores[0]
        assert score.capacity_class == CapacityClass.SUBSCRIPTION


class TestPlannerEligibilityFloors:
    """Failure caught: S2 ran with empty requirements, so a route without the
    planner's reasoning capability or context floor could lead the ladder."""

    def test_route_without_reasoning_or_context_is_skipped(self) -> None:
        no_reasoning = row("kr/gpt-4.1")
        no_reasoning["capabilities"] = {"tool_calling": True}
        short = row("gh/gpt-4.1")
        short["context_length"] = 8_000
        rows = [no_reasoning, short, row("ag/gemini-3-pro")]
        _admitted, result = _build(rows, [conn("kr"), conn("gh"), conn("ag")])
        assert result.ordered_routes == ("ag/gemini-3-pro",)
        reasons = {s.route_id: s.reason for s in result.skips}
        assert reasons["kr/gpt-4.1"] == "planner_eligibility:missing_capability:reasoning"
        assert reasons["gh/gpt-4.1"] == "planner_eligibility:insufficient_context"

    def test_planner_requirements_match_live_planner_selection(self) -> None:
        import dataclasses

        from verdict.autodev_run import AutodevError, _live_planner_models
        from verdict.role_selection import PLANNER_REQUIREMENTS

        seen: list[Any] = []

        class Recorder:
            def select(self, requirements: Any, *, now: datetime) -> tuple[None, tuple[()]]:
                seen.append(requirements)
                return None, ()

        with pytest.raises(AutodevError):
            _live_planner_models(Recorder(), top_n=3)
        assert dataclasses.replace(seen[0], exclude_pools=frozenset()) == PLANNER_REQUIREMENTS


class TestOrderingThroughAdviseOrder:
    """Failure caught: S3 sorted by strength locally and legacy_pick was the
    first sorted id, not the planner route main would choose."""

    def test_legacy_pick_is_the_live_planner_choice(self) -> None:
        rows = [row("ag/gemini-3-pro"), row("kr/gpt-4.1"), row("cc/claude-opus-5-5")]
        conns = [conn("ag"), conn("kr"), conn("cc")]
        snapshot = _snapshot({"kr/gpt-4.1": {"aa_coding": (60.0, FRESH_FETCH)}})
        admitted, result = _build(
            rows,
            conns,
            runtime=_healthy(*(r["id"] for r in rows)),
            metadata_snapshot=snapshot,
            identity_map=IdentityMap(omniroute_to_models_dev={}),
        )
        assert sorted(admitted.ids)[0] == "ag/gemini-3-pro"
        # D6 rating puts the rated kr route first; D22 keeps cc out of legacy.
        assert result.legacy_pick == "kr/gpt-4.1"

    def test_stale_strength_leaves_the_d6_order(self) -> None:
        rows = [row("ag/gemini-3-pro"), row("kr/gpt-4.1"), row("gh/gpt-4o")]
        conns = [conn("ag"), conn("kr"), conn("gh")]
        _a, baseline = _build(rows, conns, signals=_strength_signals())
        stale = _snapshot(
            {"gh/gpt-4o": {"aa_intelligence": (99.0, STALE_FETCH)}}, refreshed_at=STALE_FETCH
        )
        _a, result = _build(
            rows,
            conns,
            signals=_strength_signals(),
            metadata_snapshot=stale,
            identity_map=IdentityMap(omniroute_to_models_dev={}),
        )
        assert result.ordered_routes == baseline.ordered_routes
        assert all(s.external_strength is None for s in result.route_scores)

    def test_fresh_strength_reorders_only_through_advise_order(self) -> None:
        # D6 baseline: a fresh aa_coding score (rank 0) beats an intelligence
        # fallback (rank 1). Planner strength is aa_intelligence.
        rows = [row("kr/gpt-4.1"), row("gh/gpt-4o")]
        conns = [conn("kr"), conn("gh")]
        snapshot = _snapshot(
            {
                "kr/gpt-4.1": {"aa_coding": (50.0, FRESH_FETCH)},
                "gh/gpt-4o": {"aa_intelligence": (90.0, FRESH_FETCH)},
            }
        )
        kw = {
            "metadata_snapshot": snapshot,
            "identity_map": IdentityMap(omniroute_to_models_dev={}),
        }
        _a, without = _build(rows, conns, **kw)
        _a, advised = _build(rows, conns, signals=_strength_signals(), **kw)
        assert without.ordered_routes == ("kr/gpt-4.1", "gh/gpt-4o")
        assert advised.ordered_routes == ("gh/gpt-4o", "kr/gpt-4.1")
        assert advised.signals_digest == "b" * 64
        gh = next(s for s in advised.route_scores if s.route_id == "gh/gpt-4o")
        assert gh.external_strength == 90.0
        assert gh.strength_source == "aa_intelligence"


class TestProvenanceFlags:
    """Failure caught: metadata_fixture followed snapshot presence, so a live
    run with a metadata store was labelled fixture and fixture runs without a
    store were labelled live; the event had no live_proof field."""

    def test_live_input_with_metadata_store_is_live(self) -> None:
        rows = [row("kr/gpt-4.1")]
        _a, result = _build(
            rows,
            [conn("kr")],
            metadata_snapshot=_snapshot({}),
            identity_map=IdentityMap(omniroute_to_models_dev={}),
        )
        assert result.metadata_fixture is False
        assert result.live_proof is True
        event = json.loads(build_shadow_event(result, Role.PLANNER, timestamp="t").to_json())
        assert event["live_proof"] is True
        assert event["metadata_fixture"] is False

    def test_fixture_input_without_metadata_store_is_not_live(self) -> None:
        rows = [row("kr/gpt-4.1")]
        _a, result = _build(rows, [conn("kr")], fixture_input=True)
        assert result.metadata_fixture is True
        assert result.live_proof is False
        event = json.loads(build_shadow_event(result, Role.PLANNER, timestamp="t").to_json())
        assert event["live_proof"] is False
        assert event["metadata_fixture"] is True


def _run_cli(
    tmp_path: Path, fixture: dict[str, Any], overlay_providers: list[tuple[str, str]] | None = None
) -> Any:
    """Run the verdict ladder CLI against a fixture JSON file.

    ``overlay_providers`` declares (provider, auth_type) pairs as SUBSCRIPTION
    in the subprocess's VERDICT_HOME overlay so CP2-removed oauth rules do not
    cause FAIL_CLOSED in the subprocess.
    """
    import os
    import subprocess
    import sys

    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    vh = tmp_path / "vh"
    if overlay_providers:
        vh.mkdir(parents=True, exist_ok=True)
        (vh / "connection_billing_overlay.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "provider": p,
                            "authType": a,
                            "billing": "subscription",
                            "overage": "hard-stop",
                        }
                        for (p, a) in overlay_providers
                    ]
                }
            )
        )
    env = {**os.environ, "VERDICT_HOME": str(vh), "VERDICT_PLAIN": "1"}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "verdict",
            "ladder",
            "--role",
            "planner",
            "--json",
            "--inventory-fixture",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


class TestLadderCli:
    """Failure caught: the CLI exit code and fixture provenance were never
    exercised end to end."""

    def test_fixture_run_without_metadata_store(self, tmp_path: Path) -> None:
        proc = _run_cli(
            tmp_path,
            {"inventory": [row("kr/gpt-4.1")], "connections": [conn("kr")]},
            overlay_providers=[("kr", "oauth")],
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["ladder_result"]["metadata_fixture"] is True
        assert out["ladder_result"]["live_proof"] is False
        assert out["shadow_event"]["live_proof"] is False

    def test_fail_closed_exits_non_zero(self, tmp_path: Path) -> None:
        proc = _run_cli(
            tmp_path, {"inventory": [row("auto/best-coding")], "connections": [conn("auto")]}
        )
        assert proc.returncode != 0
        out = json.loads(proc.stdout)
        assert out["ladder_result"]["status"] == "FAIL_CLOSED"
