"""Capability ranking without price (identification correction S-A2).

Routes rank strongest first from rating evidence in Core's metadata store;
capacity class and price never order selection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.metadata import store as metadata_store
from verdict.metadata.records import (
    SOURCE_MODELS_DEV_MODELS,
    SOURCE_OPENROUTER_AA,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id
from verdict.orchestration.contracts import CapacityClass, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
FETCHED = "2026-10-20T09:00:00Z"
STALE = "2026-10-18T09:00:00Z"


def _score(value: float, fetched_at: str = FETCHED) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_OPENROUTER_AA, fetched_at=fetched_at)
    )


def _record(model_id: str, route_id: str, **scores: ProvenancedField) -> ModelMetadataRecord:
    return ModelMetadataRecord(
        id=model_id, scores=SoftScores(**scores), omniroute_ids=(route_id,), provider="x"
    )


def _snapshot(*records: ModelMetadataRecord) -> MetadataSnapshot:
    return MetadataSnapshot(schema_version="1", refreshed_at=FETCHED, sources={}, records=records)


def _ladder_row(
    route_id: str, *, pricing: dict[str, float], tier: int | None = None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": route_id,
        "owned_by": route_id.split("/", 1)[0],
        "context_length": 200_000,
        "capabilities": {"tool_calling": True},
        "pricing": pricing,
    }
    if tier is not None:
        row["capability_tier"] = tier
    return row


def _conn(provider: str, *, auth: str, free: bool = False) -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": auth,
        "isActive": True,
        "plan_label": "free" if free else "max",
        "import_free_only": free,
    }


def _ranked(ladder: EligibilityLadder, req: TaskRequirements) -> list[str]:
    verdicts = ladder.evaluate(req, now=NOW)
    ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
    return [v.route_id for v in ranked]


def _ladder(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    conns: list[dict[str, Any]],
    meta: MetadataSnapshot | None,
) -> EligibilityLadder:
    return EligibilityLadder(
        rows,
        conns,
        lambda _route: HealthResult(True, ""),
        tmp_path / "state.json",
        allow_unknown_capacity=True,
        metadata=meta,
    )


# Frontier-worthy work skips the FREE agentic-proof gate, so both routes stay eligible.
REQ = TaskRequirements(frontier_worthy=True, max_capability_tier=3)


class TestRankModels:
    def test_stronger_coding_score_ranks_first_with_provenance(self) -> None:
        from verdict.identify_rank import rank_models

        meta = _snapshot(
            _record("acme/weak", "free/weak", aa_coding=_score(30.0), aa_intelligence=_score(90.0)),
            _record("acme/strong", "sub/strong", aa_coding=_score(60.0)),
        )
        rows = rank_models(["free/weak", "sub/strong"], metadata=meta, now=NOW)
        assert [r.route_id for r in rows] == ["sub/strong", "free/weak"]
        top = rows[0]
        assert (top.score, top.metric, top.source, top.as_of, top.basis) == (
            60.0,
            "aa_coding",
            SOURCE_OPENROUTER_AA,
            FETCHED,
            "score",
        )
        assert top.provider == "sub"

    def test_general_task_uses_intelligence_metric_only(self) -> None:
        from verdict.identify_rank import rank_models

        meta = _snapshot(
            _record("acme/a", "p/a", aa_coding=_score(80.0)),
            _record("acme/b", "p/b", aa_intelligence=_score(40.0)),
        )
        rows = rank_models(["p/a", "p/b"], metadata=meta, task_family="chat", now=NOW)
        assert [(r.route_id, r.metric, r.basis) for r in rows] == [
            ("p/b", "aa_intelligence", "score"),
            ("p/a", "aa_intelligence", "unknown"),
        ]

    def test_unscored_ranks_after_every_scored_route(self) -> None:
        from verdict.identify_rank import rank_models

        meta = _snapshot(
            _record("acme/low", "p/low", aa_coding=_score(5.0)), _record("acme/none", "p/none")
        )
        rows = rank_models(
            ["p/tiered", "p/none", "p/unmapped", "p/low"],
            metadata=meta,
            declared_tiers={"p/tiered": 0},
            now=NOW,
        )
        assert [(r.route_id, r.basis) for r in rows] == [
            ("p/low", "score"),
            ("p/tiered", "tier"),
            ("p/none", "unknown"),
            ("p/unmapped", "unknown"),
        ]
        assert rows[1].tier == 0 and rows[1].score is None

    def test_stale_score_keeps_provenance_but_does_not_rank(self) -> None:
        from verdict.identify_rank import rank_models

        meta = _snapshot(
            _record("acme/old", "p/old", aa_coding=_score(99.0, STALE)),
            _record("acme/new", "p/new", aa_coding=_score(10.0)),
        )
        rows = rank_models(["p/old", "p/new"], metadata=meta, now=NOW)
        assert [r.route_id for r in rows] == ["p/new", "p/old"]
        old = rows[1]
        assert (old.score, old.as_of, old.basis) == (99.0, STALE, "unknown")

    def test_identical_inputs_give_identical_order(self) -> None:
        from verdict.identify_rank import rank_models

        meta = _snapshot(
            _record("acme/a", "p/a", aa_coding=_score(50.0)),
            _record("acme/b", "q/b", aa_coding=_score(50.0)),
        )
        ids = ["q/b", "z/none", "p/a", "a/none", "p/a"]
        first = rank_models(ids, metadata=meta, now=NOW)
        second = rank_models(list(reversed(ids)), metadata=meta, now=NOW)
        assert first == second
        assert [r.route_id for r in first] == ["p/a", "q/b", "a/none", "z/none"]

    def test_no_metadata_means_every_route_unknown(self) -> None:
        from verdict.identify_rank import rank_models

        rows = rank_models(["b/x", "a/claude-opus-5"], metadata=None, now=NOW)
        assert [(r.route_id, r.basis, r.score) for r in rows] == [
            ("a/claude-opus-5", "unknown", None),
            ("b/x", "unknown", None),
        ]


class TestLadderRanksByCapabilityNotPrice:
    def test_stronger_paid_route_beats_weaker_free_route(self, tmp_path: Path) -> None:
        rows = [
            _ladder_row("gl/model-free", pricing={"input": 0.0, "output": 0.0}),
            _ladder_row("op/model-paid", pricing={"input": 3.0, "output": 15.0}),
        ]
        conns = [_conn("gl", auth="apikey", free=True), _conn("op", auth="apikey")]
        meta = _snapshot(
            _record("acme/free", "gl/model-free", aa_coding=_score(30.0)),
            _record("acme/paid", "op/model-paid", aa_coding=_score(70.0)),
        )
        ladder = _ladder(tmp_path, rows, conns, meta)
        assert ladder.capacity_class("gl/model-free") is CapacityClass.FREE
        assert _ranked(ladder, REQ) == ["op/model-paid", "gl/model-free"]

    def test_unscored_free_route_ranks_after_scored_subscription(self, tmp_path: Path) -> None:
        rows = [
            _ladder_row("gl/model-free", pricing={"input": 0.0, "output": 0.0}),
            _ladder_row("cc/model-sub", pricing={"input": 3.0, "output": 15.0}),
        ]
        conns = [_conn("gl", auth="apikey", free=True), _conn("cc", auth="oauth")]
        meta = _snapshot(_record("acme/sub", "cc/model-sub", aa_coding=_score(20.0)))
        assert _ranked(_ladder(tmp_path, rows, conns, meta), REQ) == [
            "cc/model-sub",
            "gl/model-free",
        ]

    def test_high_score_tier0_beats_lower_score_tier1_despite_larger_slack(
        self, tmp_path: Path
    ) -> None:
        rows = [
            _ladder_row("cc/tier1", pricing={}, tier=1),
            _ladder_row("cc/tier0", pricing={}, tier=0),
        ]
        conns = [_conn("cc", auth="oauth")]
        meta = _snapshot(
            _record("acme/t1", "cc/tier1", aa_coding=_score(40.0)),
            _record("acme/t0", "cc/tier0", aa_coding=_score(65.0)),
        )
        ladder = _ladder(tmp_path, rows, conns, meta)
        verdicts = {v.route_id: v for v in ladder.evaluate(REQ, now=NOW)}
        assert verdicts["cc/tier0"].rank_components is not None
        assert verdicts["cc/tier1"].rank_components is not None
        assert (
            verdicts["cc/tier0"].rank_components["slack"]
            > verdicts["cc/tier1"].rank_components["slack"]
        )
        assert _ranked(ladder, REQ) == ["cc/tier0", "cc/tier1"]

    def test_rank_components_expose_rating_provenance(self, tmp_path: Path) -> None:
        rows = [_ladder_row("cc/model", pricing={"input": 1.0, "output": 2.0})]
        meta = _snapshot(_record("acme/m", "cc/model", aa_coding=_score(55.0)))
        ladder = _ladder(tmp_path, rows, [_conn("cc", auth="oauth")], meta)
        (verdict,) = ladder.evaluate(REQ, now=NOW)
        rc = verdict.rank_components
        assert rc is not None
        assert (rc["rating_basis"], rc["rating"], rc["rating_metric"]) == (
            "score",
            55.0,
            "aa_coding",
        )
        assert (rc["rating_source"], rc["rating_as_of"]) == (SOURCE_OPENROUTER_AA, FETCHED)

    def test_capacity_filter_still_filters(self, tmp_path: Path) -> None:
        rows = [
            _ladder_row("gl/model-free", pricing={"input": 0.0, "output": 0.0}),
            _ladder_row("op/model-paid", pricing={"input": 3.0, "output": 15.0}),
        ]
        conns = [_conn("gl", auth="apikey", free=True), _conn("op", auth="apikey")]
        ladder = _ladder(tmp_path, rows, conns, None)
        free_only = [
            rid
            for rid in ("gl/model-free", "op/model-paid")
            if ladder.capacity_class(rid).value in {"free"}
        ]
        assert free_only == ["gl/model-free"]


class TestSnapshotLookupIndexes:
    def _snapshot(self) -> MetadataSnapshot:
        cited = FieldProvenance(source=SOURCE_MODELS_DEV_MODELS, fetched_at=FETCHED)
        tools = ProvenancedField(value=True, provenance=cited)
        return MetadataSnapshot(
            schema_version="1",
            refreshed_at=FETCHED,
            sources={},
            records=(
                ModelMetadataRecord(id="acme/alias-target", omniroute_ids=("p/alias",)),
                ModelMetadataRecord(id="google/unique-leaf", caps=CapabilityCaps(tools=tools)),
                ModelMetadataRecord(id="google/shared-leaf", caps=CapabilityCaps(tools=tools)),
                ModelMetadataRecord(id="anthropic/shared-leaf", caps=CapabilityCaps(tools=tools)),
            ),
        )

    def _outcomes(self, snap: MetadataSnapshot) -> list[tuple[str | None, str | None]]:
        ids = ("p/alias", "acme/alias-target", "agy/unique-leaf", "agy/shared-leaf", "ghost/none")
        out = []
        for rid in ids:
            found = lookup_omniroute_id(snap, rid)
            out.append(
                (
                    found.record.id if found.record else None,
                    found.drop.reason if found.drop else None,
                )
            )
        return out

    def test_repeated_lookups_keep_join_semantics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snap = self._snapshot()
        expected = [
            ("acme/alias-target", None),
            ("acme/alias-target", None),
            ("google/unique-leaf", None),
            (None, "unmapped"),
            (None, "unmapped"),
        ]
        scans: list[str] = []
        real = metadata_store._record_from_models_json

        def counting(record: ModelMetadataRecord) -> bool:
            scans.append(record.id)
            return real(record)

        monkeypatch.setattr(metadata_store, "_record_from_models_json", counting)
        for _ in range(5):
            assert self._outcomes(snap) == expected
        # The leaf index is built once per snapshot, not once per lookup.
        assert len(scans) == len(snap.records)

    def test_indexes_are_per_snapshot(self) -> None:
        first = self._snapshot()
        assert lookup_omniroute_id(first, "agy/unique-leaf").record is not None
        second = MetadataSnapshot(
            schema_version="1", refreshed_at=FETCHED, sources={}, records=first.records[:1]
        )
        assert lookup_omniroute_id(second, "agy/unique-leaf").record is None
        assert lookup_omniroute_id(first, "agy/unique-leaf").record is not None
        assert first == MetadataSnapshot.from_dict(first.to_dict())


def test_stale_window_matches_metadata_doctor() -> None:
    from verdict.identify_rank import RATING_MAX_AGE

    assert timedelta(hours=12) == RATING_MAX_AGE


class TestActualSelectionRatingOrder:
    def test_stronger_route_confirms_before_weaker_cached_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict.orchestration.explain import explain_selection
        from verdict.orchestration.health_cache import HealthCache, ProbeResult

        rows = [
            _ladder_row("gl/weak", pricing={"input": 0.0, "output": 0.0}),
            _ladder_row("op/strong", pricing={"input": 3.0, "output": 15.0}),
        ]
        meta = _snapshot(
            _record("acme/weak", "gl/weak", aa_coding=_score(20.0)),
            _record("acme/strong", "op/strong", aa_coding=_score(70.0)),
        )
        monkeypatch.setattr(
            EligibilityLadder, "_rating_evidence", lambda self: (meta, None), raising=False
        )
        cache = HealthCache(tmp_path / "health.json")
        cache.record("gl/weak", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
        calls: list[str] = []

        def probe(route: str) -> HealthResult:
            calls.append(route)
            return HealthResult(True, "ok")

        ladder = EligibilityLadder(
            rows,
            [_conn("gl", auth="apikey", free=True), _conn("op", auth="apikey")],
            probe,
            tmp_path / "state.json",
            health_cache=cache,
        )
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "op/strong"
        assert selected.rank == 0
        assert calls == ["op/strong"]
        reasons = explain_selection(verdicts)["selected_because"]
        assert any("higher task rating" in reason for reason in reasons)
        assert not any("price" in reason or "capacity" in reason for reason in reasons)

    def test_stronger_failed_confirmation_falls_back_without_readmission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict.admission import RuntimeEvidence, admit
        from verdict.orchestration.health_cache import HealthCache, ProbeResult

        rows = [_ladder_row(route, pricing={}, tier=1) for route in ("cc/weak", "op/strong")]
        conns = [_conn("cc", auth="oauth"), _conn("op", auth="apikey")]
        meta = _snapshot(
            _record("acme/weak", "cc/weak", aa_coding=_score(20.0)),
            _record("acme/strong", "op/strong", aa_coding=_score(70.0)),
        )
        monkeypatch.setattr(
            EligibilityLadder, "_rating_evidence", lambda self: (meta, None), raising=False
        )
        cache = HealthCache(tmp_path / "health.json")
        cache.record("cc/weak", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
        calls: list[str] = []

        def probe(route: str) -> HealthResult:
            calls.append(route)
            return HealthResult(route == "cc/weak", "ok" if route == "cc/weak" else "unservable")

        admitted = admit(rows, conns, RuntimeEvidence(), now=NOW)
        ladder = EligibilityLadder(
            rows,
            conns,
            probe,
            tmp_path / "state.json",
            health_cache=cache,
            admitted=admitted,
            max_probes_per_select=2,
        )
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert calls == ["op/strong", "cc/weak"]
        assert selected is not None and selected.route_id == "cc/weak"
        assert "op/strong" not in ladder.admitted
        assert ladder.admitted.launchable("cc/weak")
        rejected = next(v for v in verdicts if v.route_id == "op/strong")
        assert rejected.failed_stage is not None and rejected.reason == "unservable"

    def test_hard_capability_gate_precedes_stronger_rating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            _ladder_row("cc/weak", pricing={}, tier=1),
            _ladder_row("cc/strong", pricing={}, tier=0),
        ]
        rows[1]["capabilities"] = {"tool_calling": False}
        meta = _snapshot(
            _record("acme/weak", "cc/weak", aa_coding=_score(20.0)),
            _record("acme/strong", "cc/strong", aa_coding=_score(90.0)),
        )
        ladder = _ladder(tmp_path, rows, [_conn("cc", auth="oauth")], meta)
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "cc/weak"
        rejected = next(v for v in verdicts if v.route_id == "cc/strong")
        assert rejected.rank is None and rejected.reason == "missing_capability:tools"


def test_rating_freshness_uses_each_field_not_snapshot_timestamp() -> None:
    from verdict.identify_rank import rank_models

    metadata = _snapshot(
        _record("acme/a", "p/a", aa_coding=_score(90.0, STALE), aa_intelligence=_score(70.0)),
        _record("acme/b", "p/b", aa_coding=_score(20.0)),
    )
    code = rank_models(["p/a", "p/b"], metadata=metadata, now=NOW)
    general = rank_models(["p/a", "p/b"], metadata=metadata, task_family="chat", now=NOW)
    assert [(r.route_id, r.basis) for r in code] == [("p/b", "score"), ("p/a", "unknown")]
    assert [(r.route_id, r.basis) for r in general] == [("p/a", "score"), ("p/b", "unknown")]


def test_snapshot_cached_indexes_keep_explicit_identity_map_precedence() -> None:
    from verdict.metadata.mapping import IdentityMap

    metadata = _snapshot(
        _record("acme/a", "p/alias", aa_coding=_score(10.0)),
        _record("acme/b", "p/b", aa_coding=_score(20.0)),
    )
    first = lookup_omniroute_id(metadata, "p/alias")
    mapping = IdentityMap(omniroute_to_models_dev={"p/alias": "acme/b"})
    explicit = lookup_omniroute_id(metadata, "p/alias", identity_map=mapping)
    again = lookup_omniroute_id(metadata, "p/alias")
    assert first.record is not None and first.record.id == "acme/a"
    assert explicit.record is not None and explicit.record.id == "acme/b"
    assert again.record is first.record


def test_explain_does_not_claim_rank_win_when_equal_rating_scheduler_selects_later(
    tmp_path: Path,
) -> None:
    from verdict.orchestration.explain import explain_selection
    from verdict.orchestration.health_cache import HealthCache, ProbeResult

    rows = [_ladder_row(route, pricing={}) for route in ("cc/a-cold", "cc/z-fresh")]
    meta = _snapshot(
        _record("acme/a", "cc/a-cold", aa_coding=_score(50.0)),
        _record("acme/z", "cc/z-fresh", aa_coding=_score(50.0)),
    )
    cache = HealthCache(tmp_path / "health.json")
    cache.record("cc/z-fresh", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    ladder = EligibilityLadder(
        rows,
        [_conn("cc", auth="oauth")],
        lambda route: HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=cache,
        metadata=meta,
    )
    selected, verdicts = ladder.select(REQ, now=NOW)
    assert selected is not None and selected.route_id == "cc/z-fresh" and selected.rank == 1
    explanation = explain_selection(verdicts)
    assert any("not rank order" in reason for reason in explanation["selected_because"])
    assert not any("tiebroken by route_id" in reason for reason in explanation["selected_because"])
