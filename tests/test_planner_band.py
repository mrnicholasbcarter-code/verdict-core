"""Unit tests for verdict/planner_band.py (ADR-051).

Covers:
- Frontier/fallback band computation (top 20% / 30% by default)
- Stale ratings (> 12h) are excluded from both bands
- Older-fetch ratings (not from the newest batch) are excluded even if fresh
- Unrated routes are in neither band
- Suffix variants (cc/model:free, cc/model:nitro) share one slot
- Ties at the cutoff score are included in the band
- Invalid VERDICT_PLANNER_FRONTIER_TOP / VERDICT_PLANNER_FALLBACK_TOP fail closed
- select_planner_routes: two-family rule, METERED excluded, UNKNOWN non-metered
- no_frontier_or_fallback_planner when empty
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from verdict.metadata.records import (
    SOURCE_OPENROUTER_AA,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.orchestration.contracts import CapacityClass
from verdict.planner_band import (
    BandComputation,
    PlannerBandConfig,
    PlannerBandConfigError,
    PlannerBandError,
    compute_planner_bands,
    load_planner_band_config,
    select_planner_routes,
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)
FETCHED_NEW = "2026-11-23T09:00:00Z"  # 3h before NOW → fresh, newest batch
FETCHED_OLD = "2026-11-22T09:00:00Z"  # 27h before NOW → stale (> 12h)
FETCHED_MID = "2026-11-23T06:00:00Z"  # 6h before NOW → fresh but older batch
DEFAULT_CONFIG = PlannerBandConfig(frontier_top=0.20, fallback_top=0.30)


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------


def _score(value: float, fetched_at: str = FETCHED_NEW) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_OPENROUTER_AA, fetched_at=fetched_at)
    )


def _record(
    model_id: str,
    omniroute_ids: tuple[str, ...],
    *,
    aa_intelligence: ProvenancedField | None = None,
    aa_coding: ProvenancedField | None = None,
) -> ModelMetadataRecord:
    return ModelMetadataRecord(
        id=model_id,
        scores=SoftScores(aa_intelligence=aa_intelligence, aa_coding=aa_coding),
        omniroute_ids=omniroute_ids,
        provider=omniroute_ids[0].split("/", 1)[0] if omniroute_ids else "x",
    )


def _snapshot(*records: ModelMetadataRecord) -> MetadataSnapshot:
    return MetadataSnapshot(
        schema_version="1", refreshed_at=FETCHED_NEW, sources={}, records=records
    )


def _caps_map(*route_ids: str, cap: CapacityClass = CapacityClass.FREE) -> dict[str, CapacityClass]:
    return {r: cap for r in route_ids}


# ---------------------------------------------------------------------------
# PlannerBandConfig loading
# ---------------------------------------------------------------------------


class TestLoadPlannerBandConfig:
    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VERDICT_PLANNER_FRONTIER_TOP", raising=False)
        monkeypatch.delenv("VERDICT_PLANNER_FALLBACK_TOP", raising=False)
        config = load_planner_band_config()
        assert config.frontier_top == pytest.approx(0.20)
        assert config.fallback_top == pytest.approx(0.30)

    def test_custom_values_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "0.15")
        monkeypatch.setenv("VERDICT_PLANNER_FALLBACK_TOP", "0.40")
        config = load_planner_band_config()
        assert config.frontier_top == pytest.approx(0.15)
        assert config.fallback_top == pytest.approx(0.40)

    def test_non_numeric_frontier_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "oops")
        with pytest.raises(PlannerBandConfigError, match="invalid_planner_band_config"):
            load_planner_band_config()

    def test_non_numeric_fallback_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FALLBACK_TOP", "not-a-number")
        with pytest.raises(PlannerBandConfigError, match="invalid_planner_band_config"):
            load_planner_band_config()

    def test_zero_frontier_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "0")
        with pytest.raises(PlannerBandConfigError, match="invalid_planner_band_config"):
            load_planner_band_config()

    def test_frontier_greater_than_1_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "1.5")
        with pytest.raises(PlannerBandConfigError, match="invalid_planner_band_config"):
            load_planner_band_config()

    def test_frontier_greater_than_fallback_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "0.40")
        monkeypatch.setenv("VERDICT_PLANNER_FALLBACK_TOP", "0.20")
        with pytest.raises(PlannerBandConfigError, match="invalid_planner_band_config"):
            load_planner_band_config()

    def test_equal_frontier_and_fallback_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "0.25")
        monkeypatch.setenv("VERDICT_PLANNER_FALLBACK_TOP", "0.25")
        config = load_planner_band_config()
        assert config.frontier_top == config.fallback_top


# ---------------------------------------------------------------------------
# compute_planner_bands: basic frontier/fallback membership
# ---------------------------------------------------------------------------


class TestComputePlannerBands:
    def test_no_metadata_returns_empty_bands(self) -> None:
        band = compute_planner_bands(
            ["cc/a", "oa/b"], metadata=None, identity_map=None, config=DEFAULT_CONFIG, now=NOW
        )
        assert len(band.frontier_model_keys) == 0
        assert len(band.fallback_model_keys) == 0
        assert band.rated_models == 0

    def test_frontier_top_20pct_ceil(self) -> None:
        # 10 routes → ceil(10 * 0.20) = 2 → top 2 in frontier
        records = [
            _record(f"p/model{i}", (f"cc/model{i}",), aa_intelligence=_score(float(10 - i)))
            for i in range(10)
        ]
        snap = _snapshot(*records)
        inventory = [f"cc/model{i}" for i in range(10)]
        band = compute_planner_bands(inventory, snap, None, config=DEFAULT_CONFIG, now=NOW)
        assert band.rated_models == 10
        assert len(band.frontier_model_keys) == 2
        # top 2 by score: model0 (10.0), model1 (9.0)
        assert "cc/model0" in band.frontier_model_keys
        assert "cc/model1" in band.frontier_model_keys
        assert "cc/model2" not in band.frontier_model_keys

    def test_fallback_top_30pct_ceil(self) -> None:
        # 10 routes → ceil(10 * 0.30) = 3 → top 3 in fallback
        records = [
            _record(f"p/model{i}", (f"cc/model{i}",), aa_intelligence=_score(float(10 - i)))
            for i in range(10)
        ]
        snap = _snapshot(*records)
        inventory = [f"cc/model{i}" for i in range(10)]
        band = compute_planner_bands(inventory, snap, None, config=DEFAULT_CONFIG, now=NOW)
        assert len(band.fallback_model_keys) == 3
        assert "cc/model0" in band.fallback_model_keys
        assert "cc/model2" in band.fallback_model_keys
        assert "cc/model3" not in band.fallback_model_keys

    def test_frontier_subset_of_fallback(self) -> None:
        records = [
            _record(f"p/m{i}", (f"cc/m{i}",), aa_intelligence=_score(float(10 - i)))
            for i in range(10)
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            [f"cc/m{i}" for i in range(10)], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        assert band.frontier_model_keys <= band.fallback_model_keys

    def test_ties_at_cutoff_included_in_frontier(self) -> None:
        # 5 routes, top 1 = ceil(5*0.20) = 1
        # but if 2 routes share the top score, both must be in frontier
        records = [
            _record("p/a", ("cc/a",), aa_intelligence=_score(90.0)),
            _record("p/b", ("cc/b",), aa_intelligence=_score(90.0)),  # tie with a
            _record("p/c", ("cc/c",), aa_intelligence=_score(80.0)),
            _record("p/d", ("cc/d",), aa_intelligence=_score(70.0)),
            _record("p/e", ("cc/e",), aa_intelligence=_score(60.0)),
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            ["cc/a", "cc/b", "cc/c", "cc/d", "cc/e"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        # ceil(5 * 0.20) = 1, but score[0] == score[1] = 90.0, so both included
        assert "cc/a" in band.frontier_model_keys
        assert "cc/b" in band.frontier_model_keys
        assert len(band.frontier_model_keys) == 2  # tie expanded

    def test_ties_at_fallback_cutoff_included(self) -> None:
        # 10 models, fallback = top 3; model3 ties model2
        scores = [90, 85, 75, 75, 70, 65, 60, 55, 50, 45]
        records = [
            _record(f"p/m{i}", (f"cc/m{i}",), aa_intelligence=_score(float(scores[i])))
            for i in range(10)
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            [f"cc/m{i}" for i in range(10)], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        # ceil(10*0.30) = 3; model2 and model3 both have 75.0 = cutoff
        assert "cc/m2" in band.fallback_model_keys
        assert "cc/m3" in band.fallback_model_keys
        # model4 (70.0) < cutoff (75.0) → out
        assert "cc/m4" not in band.fallback_model_keys

    def test_unrated_route_in_neither_band(self) -> None:
        # Only two routes rated; unrated is not in population
        snap = _snapshot(_record("p/rated", ("cc/rated",), aa_intelligence=_score(90.0)))
        band = compute_planner_bands(
            ["cc/rated", "cc/unrated"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        assert "cc/unrated" not in band.frontier_model_keys
        assert "cc/unrated" not in band.fallback_model_keys
        assert band.rated_models == 1

    def test_stale_rating_excluded(self) -> None:
        # Score fetched_at is > 12h before NOW → stale → neither band
        snap = _snapshot(
            _record("p/stale", ("cc/stale",), aa_intelligence=_score(99.0, FETCHED_OLD))
        )
        band = compute_planner_bands(["cc/stale"], snap, None, config=DEFAULT_CONFIG, now=NOW)
        assert band.rated_models == 0
        assert len(band.frontier_model_keys) == 0

    def test_older_fetch_excluded_even_if_fresh(self) -> None:
        # Two routes: one with FETCHED_NEW (3h ago), one with FETCHED_MID (6h ago)
        # Both are within 12h (fresh), but only FETCHED_NEW is the newest batch
        snap = _snapshot(
            _record("p/new", ("cc/new",), aa_intelligence=_score(90.0, FETCHED_NEW)),
            _record("p/mid", ("cc/mid",), aa_intelligence=_score(95.0, FETCHED_MID)),
        )
        band = compute_planner_bands(
            ["cc/new", "cc/mid"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        # Only newest batch (FETCHED_NEW) → cc/new stays, cc/mid excluded
        # even though cc/mid would have ranked higher by score
        assert "cc/new" in band.frontier_model_keys
        assert "cc/mid" not in band.frontier_model_keys
        assert band.rated_models == 1
        assert band.fetched_at == FETCHED_NEW

    def test_suffix_variants_share_one_slot(self) -> None:
        # cc/model:free and cc/model:nitro both strip to cc/model → one slot
        snap = _snapshot(_record("p/model", ("cc/model",), aa_intelligence=_score(90.0)))
        # Inventory has suffix variants but no extra records
        band = compute_planner_bands(
            ["cc/model:free", "cc/model:nitro", "cc/model"],
            snap,
            None,
            config=DEFAULT_CONFIG,
            now=NOW,
        )
        # All three map to one slot: cc/model
        assert band.rated_models == 1
        assert "cc/model" in band.frontier_model_keys

    def test_suffix_variants_in_admitted_routes_match_band(self) -> None:
        # Admitted routes include suffix variants; they should check as model key
        from verdict.planner_band import _strip_suffix

        assert _strip_suffix("cc/model:free") == "cc/model"
        assert _strip_suffix("cc/model:nitro") == "cc/model"
        assert _strip_suffix("cc/model") == "cc/model"
        assert _strip_suffix("oa/gpt-4:turbo") == "oa/gpt-4"

    def test_single_route_in_frontier_and_fallback(self) -> None:
        snap = _snapshot(_record("p/only", ("cc/only",), aa_intelligence=_score(80.0)))
        band = compute_planner_bands(["cc/only"], snap, None, config=DEFAULT_CONFIG, now=NOW)
        assert "cc/only" in band.frontier_model_keys
        assert "cc/only" in band.fallback_model_keys
        assert band.rated_models == 1

    def test_records_scores_and_ranks(self) -> None:
        records = [
            _record("p/top", ("cc/top",), aa_intelligence=_score(95.0)),
            _record("p/mid", ("cc/mid",), aa_intelligence=_score(85.0)),
            _record("p/bot", ("cc/bot",), aa_intelligence=_score(75.0)),
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            ["cc/top", "cc/mid", "cc/bot"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        assert band.model_scores["cc/top"] == pytest.approx(95.0)
        assert band.model_scores["cc/mid"] == pytest.approx(85.0)
        assert band.model_ranks["cc/top"] == 1
        assert band.model_ranks["cc/mid"] == 2
        assert band.model_ranks["cc/bot"] == 3

    def test_cutoffs_match_band_boundary(self) -> None:
        # 5 routes, scores 90, 80, 70, 60, 50
        # frontier = ceil(5*0.20)=1 → cutoff=90; fallback=ceil(5*0.30)=2 → cutoff=80
        records = [
            _record("p/a", ("cc/a",), aa_intelligence=_score(90.0)),
            _record("p/b", ("cc/b",), aa_intelligence=_score(80.0)),
            _record("p/c", ("cc/c",), aa_intelligence=_score(70.0)),
            _record("p/d", ("cc/d",), aa_intelligence=_score(60.0)),
            _record("p/e", ("cc/e",), aa_intelligence=_score(50.0)),
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            ["cc/a", "cc/b", "cc/c", "cc/d", "cc/e"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        assert band.cutoff_frontier == pytest.approx(90.0)
        assert band.cutoff_fallback == pytest.approx(80.0)

    def test_fractional_top_1_always_includes_at_least_one(self) -> None:
        # 1 route → ceil(1 * 0.20) = 1 → route is in both bands
        snap = _snapshot(_record("p/solo", ("cc/solo",), aa_intelligence=_score(50.0)))
        band = compute_planner_bands(["cc/solo"], snap, None, config=DEFAULT_CONFIG, now=NOW)
        assert "cc/solo" in band.frontier_model_keys


# ---------------------------------------------------------------------------
# select_planner_routes
# ---------------------------------------------------------------------------


class TestSelectPlannerRoutes:
    def _two_family_band(self) -> tuple[BandComputation, MetadataSnapshot]:
        """10 routes across 2 families; top 2 in frontier, top 3 in fallback.

        Uses recognizable family names ('claude' and 'gpt') so route_family()
        returns distinct values for the two-family rule.
        """
        records = [
            _record(
                "p/c0", ("cc/claude-3-opus",), aa_intelligence=_score(95.0), aa_coding=_score(80.0)
            ),
            _record(
                "p/c1",
                ("cc/claude-3-sonnet",),
                aa_intelligence=_score(85.0),
                aa_coding=_score(70.0),
            ),
            _record("p/c2", ("cc/claude-3-haiku",), aa_intelligence=_score(75.0)),
            _record("p/c3", ("cc/claude-3-mini",), aa_intelligence=_score(65.0)),
            _record("p/c4", ("cc/claude-instant",), aa_intelligence=_score(55.0)),
            _record("p/g0", ("oa/gpt-4o",), aa_intelligence=_score(90.0), aa_coding=_score(85.0)),
            _record("p/g1", ("oa/gpt-4-turbo",), aa_intelligence=_score(80.0)),
            _record("p/g2", ("oa/gpt-4",), aa_intelligence=_score(70.0)),
            _record("p/g3", ("oa/gpt-3.5-turbo",), aa_intelligence=_score(60.0)),
            _record("p/g4", ("oa/gpt-3.5",), aa_intelligence=_score(50.0)),
        ]
        snap = _snapshot(*records)
        cc_routes = [
            "cc/claude-3-opus",
            "cc/claude-3-sonnet",
            "cc/claude-3-haiku",
            "cc/claude-3-mini",
            "cc/claude-instant",
        ]
        oa_routes = ["oa/gpt-4o", "oa/gpt-4-turbo", "oa/gpt-4", "oa/gpt-3.5-turbo", "oa/gpt-3.5"]
        inventory = cc_routes + oa_routes
        band = compute_planner_bands(inventory, snap, None, config=DEFAULT_CONFIG, now=NOW)
        return band, snap

    def test_selects_primary_from_frontier(self) -> None:
        band, snap = self._two_family_band()
        # frontier = top 2 of 10: cc/claude-3-opus (95), oa/gpt-4o (90)
        admitted = ["cc/claude-3-opus", "oa/gpt-4o", "cc/claude-3-sonnet"]
        caps = _caps_map("cc/claude-3-opus", "oa/gpt-4o", "cc/claude-3-sonnet")
        selected = select_planner_routes(admitted, caps, band, snap, None, top_n=1, now=NOW)
        assert len(selected) == 1
        route_id, result = selected[0]
        # cc/claude-3-opus has highest score (95) → primary
        assert route_id == "cc/claude-3-opus"
        assert result.used == "frontier"
        assert result.required == "frontier"
        assert result.reason is None

    def test_two_family_rule_selects_different_families(self) -> None:
        band, snap = self._two_family_band()
        # Both are in frontier: cc/claude-3-opus (95), oa/gpt-4o (90)
        admitted = ["cc/claude-3-opus", "oa/gpt-4o"]
        caps = _caps_map("cc/claude-3-opus", "oa/gpt-4o")
        selected = select_planner_routes(admitted, caps, band, snap, None, top_n=2, now=NOW)
        assert len(selected) == 2
        families = {r.split("/")[0] for r, _ in selected}
        assert families == {"cc", "oa"}

    def test_failover_uses_fallback_when_only_frontier_one_family(self) -> None:
        # Only claude in frontier; gpt only in fallback.
        # With 10 routes: frontier = ceil(10*0.20)=2; fallback = ceil(10*0.30)=3.
        records2 = [
            _record("p/c0", ("cc/claude-3-opus",), aa_intelligence=_score(95.0)),
            _record("p/c1", ("cc/claude-3-sonnet",), aa_intelligence=_score(90.0)),
            _record("p/c2", ("cc/claude-3-haiku",), aa_intelligence=_score(80.0)),
            _record("p/c3", ("cc/claude-instant",), aa_intelligence=_score(70.0)),
            _record("p/c4", ("cc/claude-2",), aa_intelligence=_score(60.0)),
            _record("p/g0", ("oa/gpt-4o",), aa_intelligence=_score(85.0)),
            _record("p/g1", ("oa/gpt-4-turbo",), aa_intelligence=_score(75.0)),
            _record("p/g2", ("oa/gpt-4",), aa_intelligence=_score(65.0)),
            _record("p/g3", ("oa/gpt-3.5-turbo",), aa_intelligence=_score(55.0)),
            _record("p/g4", ("oa/gpt-3.5",), aa_intelligence=_score(45.0)),
        ]
        snap2 = _snapshot(*records2)
        # 10 routes; frontier = ceil(10*0.20) = 2; fallback = ceil(10*0.30) = 3
        # Sorted by score: c0=95, g0=85, c1=90, ... wait let me recalculate
        # Scores desc: c0=95, c1=90, g0=85, g1=75, c2=80... no:
        # c0=95, c1=90, c2=80, g0=85, g1=75, c3=70, g2=65, c4=60, g3=55, g4=45
        # Sorted: 95(c0), 90(c1), 85(g0), 80(c2), 75(g1), ...
        # Frontier (top 2): cc/claude-3-opus (95), cc/claude-3-sonnet (90)
        # Fallback (top 3): cc/claude-3-opus (95), cc/claude-3-sonnet (90), oa/gpt-4o (85)
        all_routes = [
            "cc/claude-3-opus",
            "cc/claude-3-sonnet",
            "cc/claude-3-haiku",
            "cc/claude-instant",
            "cc/claude-2",
            "oa/gpt-4o",
            "oa/gpt-4-turbo",
            "oa/gpt-4",
            "oa/gpt-3.5-turbo",
            "oa/gpt-3.5",
        ]
        band2 = compute_planner_bands(all_routes, snap2, None, config=DEFAULT_CONFIG, now=NOW)
        # Admit: one claude (frontier) + gpt-4o (only in fallback)
        admitted = ["cc/claude-3-opus", "oa/gpt-4o"]
        caps = _caps_map("cc/claude-3-opus", "oa/gpt-4o")
        selected = select_planner_routes(admitted, caps, band2, snap2, None, top_n=2, now=NOW)
        assert len(selected) == 2
        route_ids = [r for r, _ in selected]
        assert "cc/claude-3-opus" in route_ids
        assert "oa/gpt-4o" in route_ids
        # failover route uses fallback
        fallback_sel = [res for r, res in selected if r == "oa/gpt-4o"]
        assert fallback_sel[0].used == "fallback"
        assert fallback_sel[0].reason == "no_frontier_admissible"

    def test_raises_when_no_band_routes_admitted(self) -> None:
        band, snap = self._two_family_band()
        # Only routes NOT in bands (bottom-ranked)
        admitted = ["cc/claude-instant", "oa/gpt-3.5"]
        caps = _caps_map("cc/claude-instant", "oa/gpt-3.5")
        with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
            select_planner_routes(admitted, caps, band, snap, None, top_n=1, now=NOW)

    def test_metered_routes_excluded_from_selection(self) -> None:
        # Need both routes in the band: use 10 routes so top-2 are both in frontier
        records = [
            _record("p/c0", ("cc/claude-3-opus",), aa_intelligence=_score(95.0)),
            _record("p/c1", ("cc/claude-3-sonnet",), aa_intelligence=_score(85.0)),
            _record("p/c2", ("cc/claude-3-haiku",), aa_intelligence=_score(75.0)),
            _record("p/c3", ("cc/claude-instant",), aa_intelligence=_score(65.0)),
            _record("p/c4", ("cc/claude-2",), aa_intelligence=_score(55.0)),
            _record("p/c5", ("cc/claude-v1",), aa_intelligence=_score(45.0)),
            _record("p/c6", ("cc/claude-v1-mini",), aa_intelligence=_score(35.0)),
            _record("p/c7", ("cc/claude-v1-100k",), aa_intelligence=_score(25.0)),
            _record("p/c8", ("cc/claude-v1-instant",), aa_intelligence=_score(15.0)),
            _record("p/c9", ("cc/claude-v1-instant-100k",), aa_intelligence=_score(5.0)),
        ]
        snap = _snapshot(*records)
        inventory = [
            "cc/claude-3-opus",
            "cc/claude-3-sonnet",
            "cc/claude-3-haiku",
            "cc/claude-instant",
            "cc/claude-2",
            "cc/claude-v1",
            "cc/claude-v1-mini",
            "cc/claude-v1-100k",
            "cc/claude-v1-instant",
            "cc/claude-v1-instant-100k",
        ]
        band = compute_planner_bands(inventory, snap, None, config=DEFAULT_CONFIG, now=NOW)
        # frontier = top 2: cc/claude-3-opus (95), cc/claude-3-sonnet (85)
        # cc/claude-3-opus is METERED → selection falls through to cc/claude-3-sonnet (FREE)
        caps = {"cc/claude-3-opus": CapacityClass.METERED, "cc/claude-3-sonnet": CapacityClass.FREE}
        selected = select_planner_routes(
            ["cc/claude-3-opus", "cc/claude-3-sonnet"], caps, band, snap, None, top_n=1, now=NOW
        )
        route_id, _ = selected[0]
        assert route_id == "cc/claude-3-sonnet"  # cc/claude-3-opus (METERED) skipped

    def test_metered_only_raises(self) -> None:
        records = [_record("p/cc0", ("cc/model0",), aa_intelligence=_score(95.0))]
        snap = _snapshot(*records)
        band = compute_planner_bands(["cc/model0"], snap, None, config=DEFAULT_CONFIG, now=NOW)
        caps = {"cc/model0": CapacityClass.METERED}
        with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
            select_planner_routes(["cc/model0"], caps, band, snap, None, top_n=1, now=NOW)

    def test_unknown_capacity_treated_as_non_metered(self) -> None:
        records = [_record("p/unk", ("cc/unk",), aa_intelligence=_score(90.0))]
        snap = _snapshot(*records)
        band = compute_planner_bands(["cc/unk"], snap, None, config=DEFAULT_CONFIG, now=NOW)
        caps = {"cc/unk": CapacityClass.UNKNOWN}
        selected = select_planner_routes(["cc/unk"], caps, band, snap, None, top_n=1, now=NOW)
        assert len(selected) == 1
        route_id, _ = selected[0]
        assert route_id == "cc/unk"  # UNKNOWN is non-metered → eligible

    def test_free_before_subscription_in_selection(self) -> None:
        # Both routes in frontier; FREE should be preferred over SUBSCRIPTION
        # when scores are equal
        # cc/free and cc/sub share a stem; force different families by using
        # different provider prefixes.
        records2 = [
            _record("p/freemod", ("cc/free",), aa_intelligence=_score(90.0)),
            _record("p/submod", ("oa/sub",), aa_intelligence=_score(90.0)),
        ]
        snap2 = _snapshot(*records2)
        band2 = compute_planner_bands(
            ["cc/free", "oa/sub"], snap2, None, config=DEFAULT_CONFIG, now=NOW
        )
        caps2 = {"cc/free": CapacityClass.FREE, "oa/sub": CapacityClass.SUBSCRIPTION}
        selected = select_planner_routes(
            ["cc/free", "oa/sub"], caps2, band2, snap2, None, top_n=1, now=NOW
        )
        route_id, _ = selected[0]
        assert route_id == "cc/free"  # FREE before SUBSCRIPTION at same score

    def test_fewer_than_two_families_raises_for_top_n_2(self) -> None:
        # Only one family (claude) available across both bands
        records = [
            _record("p/a", ("cc/claude-3-opus",), aa_intelligence=_score(95.0)),
            _record("p/b", ("cc/claude-3-sonnet",), aa_intelligence=_score(85.0)),
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            ["cc/claude-3-opus", "cc/claude-3-sonnet"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        caps = _caps_map("cc/claude-3-opus", "cc/claude-3-sonnet")
        with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
            select_planner_routes(
                ["cc/claude-3-opus", "cc/claude-3-sonnet"], caps, band, snap, None, top_n=2, now=NOW
            )

    def test_band_result_fields_populated(self) -> None:
        records = [_record("p/c0", ("cc/claude-3-opus",), aa_intelligence=_score(95.0))]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            ["cc/claude-3-opus"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        caps = {"cc/claude-3-opus": CapacityClass.FREE}
        selected = select_planner_routes(
            ["cc/claude-3-opus"], caps, band, snap, None, top_n=1, now=NOW
        )
        _, result = selected[0]
        assert result.required == "frontier"
        assert result.used == "frontier"
        assert result.reason is None
        assert result.rank == 1
        assert result.rated_models == 1
        assert result.rating == pytest.approx(95.0)
        assert result.metric == "aa_intelligence"
        assert result.fetched_at == FETCHED_NEW
        d = result.to_dict()
        assert set(d.keys()) == {
            "required",
            "used",
            "reason",
            "rank",
            "rated_models",
            "cutoff",
            "rating",
            "metric",
            "fetched_at",
        }

    def test_suffix_variant_matched_in_selection(self) -> None:
        # Admitted routes include suffix variants; band uses stripped key
        snap = _snapshot(_record("p/claude", ("cc/claude-3",), aa_intelligence=_score(95.0)))
        band = compute_planner_bands(
            ["cc/claude-3", "cc/claude-3:free"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        caps = {"cc/claude-3:free": CapacityClass.FREE}
        # cc/claude-3:free strips to cc/claude-3 → matches frontier band
        selected = select_planner_routes(
            ["cc/claude-3:free"], caps, band, snap, None, top_n=1, now=NOW
        )
        assert len(selected) == 1
        assert selected[0][0] == "cc/claude-3:free"

    def test_non_admitted_route_not_added(self) -> None:
        # Band has routes that are not in admitted set; they are not added
        band, snap = self._two_family_band()
        # Empty admitted set → nothing to select from
        admitted: list[str] = []
        caps: dict[str, CapacityClass] = {}
        with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
            select_planner_routes(admitted, caps, band, snap, None, top_n=1, now=NOW)


# ---------------------------------------------------------------------------
# planner_supply_status helper
# ---------------------------------------------------------------------------


class TestPlannerSupplyStatus:
    def test_ok_when_frontier_has_routes(self) -> None:
        from verdict.planner_band import planner_supply_status

        snap = _snapshot(_record("p/a", ("cc/a",), aa_intelligence=_score(95.0)))
        band = compute_planner_bands(["cc/a"], snap, None, config=DEFAULT_CONFIG, now=NOW)
        caps = {"cc/a": CapacityClass.FREE}
        status = planner_supply_status(["cc/a"], caps, band)
        assert status["frontier_admissible"] == 1
        assert status["warning"] is False

    def test_warn_when_no_frontier(self) -> None:
        from verdict.planner_band import planner_supply_status

        # 2 routes; fallback has both but frontier has only top 20% → 1
        # Admit only the route NOT in frontier
        records = [
            _record("p/a", ("cc/a",), aa_intelligence=_score(90.0)),
            _record("p/b", ("cc/b",), aa_intelligence=_score(80.0)),
            _record("p/c", ("cc/c",), aa_intelligence=_score(70.0)),
            _record("p/d", ("cc/d",), aa_intelligence=_score(60.0)),
            _record("p/e", ("cc/e",), aa_intelligence=_score(50.0)),
        ]
        snap = _snapshot(*records)
        # 5 routes → frontier = ceil(5*0.20) = 1 (cc/a); fallback = ceil(5*0.30) = 2 (cc/a, cc/b)
        band = compute_planner_bands(
            ["cc/a", "cc/b", "cc/c", "cc/d", "cc/e"], snap, None, config=DEFAULT_CONFIG, now=NOW
        )
        # Only admit routes NOT in frontier
        caps = {r: CapacityClass.FREE for r in ["cc/b", "cc/c"]}
        status = planner_supply_status(["cc/b", "cc/c"], caps, band)
        assert status["frontier_admissible"] == 0
        assert status["warning"] is True


# ---------------------------------------------------------------------------
# rating_coverage_summary helper
# ---------------------------------------------------------------------------


class TestRatingCoverageSummary:
    def test_coverage_summary_fields(self) -> None:
        from verdict.planner_band import rating_coverage_summary

        records = [
            _record("p/a", ("cc/a",), aa_intelligence=_score(95.0)),
            _record("p/b", ("cc/b",), aa_intelligence=_score(85.0)),
        ]
        snap = _snapshot(*records)
        band = compute_planner_bands(
            ["cc/a", "cc/b", "cc/c"],  # cc/c unrated
            snap,
            None,
            config=DEFAULT_CONFIG,
            now=NOW,
        )
        summary = rating_coverage_summary(["cc/a", "cc/b", "cc/c"], band, snap, None, now=NOW)
        assert summary["rated_models"] == 2
        assert summary["inventory_routes"] == 3
        assert summary["coverage_pct"] == pytest.approx(66.7)
        assert "frontier_model_count" in summary
        assert "fallback_model_count" in summary
