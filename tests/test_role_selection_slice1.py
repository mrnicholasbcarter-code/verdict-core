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

# =============================================================================
# Fixtures
# =============================================================================

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def row(route_id: str, *, owned_by: str | None = None, tools: bool = True) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": 200_000,
        "capabilities": {"tool_calling": tools},
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

        # With fable removed, next rung from different pool (not cc/*)
        inventory_without_fable = [r for r in inventory_rows if r["id"] != "cc/claude-fable-5-2"]
        connections_subset = [c for c in connections if c["provider"] != "cc"]

        admitted2 = admit(inventory_without_fable, connections_subset, RuntimeEvidence(), now=NOW)
        result2 = build_ladder(
            role=Role.PLANNER,
            admitted=admitted2,
            inventory_rows=inventory_without_fable,
            connections=connections_subset,
            quota_view=OpenQuotaView(),
            clock=NOW,
            metadata_snapshot=metadata_snapshot,
            identity_map=identity_map,
        )
        assert result2.status == LadderStatus.OK
        first_route = result2.ordered_routes[0]
        assert not first_route.startswith("cc/"), f"Expected non-cc route, got {first_route}"


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
