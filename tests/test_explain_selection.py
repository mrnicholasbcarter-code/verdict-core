"""BOD-277: RouteVerdict rank_components and data-only selection explanation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    RouteVerdict,
    TaskRequirements,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.explain import explain_selection
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


def _row(
    route_id: str,
    *,
    owned_by: str | None = None,
    context: int = 200_000,
    tools: bool = True,
    structured: bool = True,
    reasoning: bool = True,
    pricing: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    caps = {"tool_calling": tools, "reasoning": reasoning, "structured_output": structured}
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": context,
        "max_input_tokens": context,
        "max_output_tokens": 32_000,
        "capabilities": caps,
        "pricing": dict(pricing) if pricing is not None else {"input": 1.0, "output": 2.0},
    }


def _conn(provider: str, *, plan: str = "max") -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": "oauth",
        "isActive": True,
        "testStatus": "ok",
        "backoffLevel": 0,
        "plan_label": plan,
        "rate_limited_until": None,
        "import_free_only": False,
    }


class _Probe:
    def __init__(self, results: Mapping[str, HealthResult] | None = None) -> None:
        self._results = dict(results or {})

    def __call__(self, route_id: str) -> HealthResult:
        return self._results.get(route_id, HealthResult(healthy=True, category=""))


def _ladder(
    tmp_path: Path, rows: list[dict[str, Any]], conns: list[dict[str, Any]], *, load: Any = None
) -> EligibilityLadder:
    return EligibilityLadder(
        inventory_rows=rows,
        connections=conns,
        probe=_Probe(),
        state_path=tmp_path / "elig.json",
        load=load,
    )


# ---------------------------------------------------------------------------
# 1. RouteVerdict additive fields on to_dict()
# ---------------------------------------------------------------------------


class TestRouteVerdictAdditive:
    def test_bare_verdict_keeps_base_keys_and_unknown_provenance(self) -> None:
        v = RouteVerdict(
            route_id="p/m",
            provider="p",
            reached=EligibilityStage.HEALTHY,
            failed_stage=EligibilityStage.AVAILABLE,
            reason="cooldown",
        )
        d = v.to_dict()
        assert set(d) == {
            "route_id",
            "provider",
            "reached",
            "failed_stage",
            "reason",
            "capacity_class",
            "plan_label",
            "cooldown_until",
            "rank",
            "pool",
            "capacity_evidence",
            "cooldown_scope",
        }
        assert d["pool"] == d["capacity_evidence"] == d["cooldown_scope"] == ""

    def test_optional_keys_added_only_when_populated(self) -> None:
        v = RouteVerdict(
            route_id="p/m",
            provider="p",
            reached=EligibilityStage.SELECTED,
            failed_stage=None,
            reason="selected",
            rank=0,
            rank_components={
                "capacity_order": 0,
                "slack": 0,
                "price": None,
                "provider_pref": 0,
                "load": 0,
                "fit": 2,
                "route_id": "p/m",
            },
            capability_tier=1,
            context_window=200_000,
            supports_tools=True,
            supports_structured_output=False,
            price=None,
        )
        d = v.to_dict()
        assert d["rank_components"]["price"] is None
        assert d["capability_tier"] == 1
        assert d["context_window"] == 200_000
        assert d["supports_tools"] is True
        assert d["supports_structured_output"] is False
        assert "price" not in d  # None price is omitted (UNKNOWN)


# ---------------------------------------------------------------------------
# 2. rank_components mirror the real selector
# ---------------------------------------------------------------------------


class TestRankComponentsMirrorSelector:
    def test_sorting_on_components_reproduces_selector_order(self, tmp_path: Path) -> None:
        # Three sufficient candidates with different price and load. Real
        # selector should pick the cheapest first, and sorting on the exposed
        # tuple must match. ``owned_by`` mirrors the provider the connection
        # is keyed on (``EligibilityLadder._connection_for`` compares them).
        rows = [
            _row("cc/claude-haiku-5", owned_by="claude", pricing={"input": 0.25, "output": 1.25}),
            _row("cc/claude-sonnet-5", owned_by="claude", pricing={"input": 3.0, "output": 15.0}),
            _row("cc/claude-opus-5", owned_by="claude", pricing={"input": 15.0, "output": 75.0}),
        ]
        ladder = _ladder(tmp_path, rows, [_conn("claude")])
        req = TaskRequirements(coding=False)
        chosen, verdicts = ladder.select(req, now=NOW)
        assert chosen is not None
        ranked = [v for v in verdicts if v.rank is not None]

        # Reconstruct the sort tuple from exposed values only (price is not a key).
        def key(v: RouteVerdict) -> tuple[Any, ...]:
            rc = v.rank_components or {}
            return (
                ("score", "intelligence_fallback", "tier", "unknown").index(
                    rc.get("rating_basis", "unknown")
                ),
                -(rc.get("rating") or 0.0),
                rc.get("declared_tier") or 0,
                rc.get("slack"),
                rc.get("quota_rank"),
                rc.get("provider_pref"),
                rc.get("load"),
                -int(rc.get("fit", 0)),
                rc.get("route_id"),
            )

        by_rank = sorted(ranked, key=lambda v: v.rank if v.rank is not None else 0)
        by_components = sorted(ranked, key=key)
        assert [v.route_id for v in by_rank] == [v.route_id for v in by_components]
        # And the selected route is the first by both orderings.
        assert chosen.route_id == by_components[0].route_id

    def test_load_affects_ranking_and_is_exposed(self, tmp_path: Path) -> None:
        rows = [
            _row("cc/claude-haiku-5", owned_by="claude", pricing={"input": 1.0, "output": 2.0}),
            _row("cc/claude-mini-5", owned_by="claude", pricing={"input": 1.0, "output": 2.0}),
        ]
        loads = {"cc/claude-haiku-5": 1}
        ladder = _ladder(tmp_path, rows, [_conn("claude")], load=lambda r: loads.get(r, 0))
        req = TaskRequirements(coding=False)
        chosen, verdicts = ladder.select(req, now=NOW)
        assert chosen is not None
        rc_by_route = {v.route_id: v.rank_components for v in verdicts if v.rank_components}
        assert rc_by_route["cc/claude-haiku-5"]["load"] == 1
        assert rc_by_route["cc/claude-mini-5"]["load"] == 0
        # Lower-load one wins the tie.
        assert chosen.route_id == "cc/claude-mini-5"


# ---------------------------------------------------------------------------
# 3. Unknown values -> None (never fabricated defaults)
# ---------------------------------------------------------------------------


class TestUnknownValuesAreNone:
    def test_no_pricing_row_reports_price_none(self, tmp_path: Path) -> None:
        row = _row("cc/claude-x", owned_by="claude")
        del row["pricing"]  # no pricing metadata at all
        ladder = _ladder(tmp_path, [row], [_conn("claude")])
        req = TaskRequirements(coding=False)
        _, verdicts = ladder.select(req, now=NOW)
        sel = next(v for v in verdicts if v.reached is EligibilityStage.SELECTED)
        assert sel.price is None
        assert (sel.rank_components or {})["price"] is None
        d = sel.to_dict()
        # to_dict omits the top-level price key when unknown
        assert "price" not in d
        # ...but keeps the rank_components price entry so consumers see the sort key.
        assert d["rank_components"]["price"] is None

    def test_missing_capability_flags_are_none(self, tmp_path: Path) -> None:
        row = _row("cc/claude-x", owned_by="claude")
        row["capabilities"] = {"tool_calling": True}  # no structured_output key
        row["max_input_tokens"] = 0  # unknown context window
        row["context_length"] = 0
        # min_context_tokens=0 so unknown context does not fail TASK_ELIGIBLE
        ladder = _ladder(tmp_path, [row], [_conn("claude")])
        req = TaskRequirements(coding=False, min_context_tokens=0)
        _, verdicts = ladder.select(req, now=NOW)
        sel = next(v for v in verdicts if v.reached is EligibilityStage.SELECTED)
        assert sel.supports_tools is True
        assert sel.supports_structured_output is None
        assert sel.context_window is None


# ---------------------------------------------------------------------------
# 4. Behaviour parity: exposing values does not change selection
# ---------------------------------------------------------------------------


class TestBehaviourParityAcrossFixtures:
    """The pre-BOD-277 selection order must be unchanged by additive fields."""

    def _pre_bod277_order(
        self, tmp_path: Path, rows: list[dict[str, Any]], conns: list[dict[str, Any]]
    ) -> tuple[str | None, list[str]]:
        """Compute the selection outcome using only fields that existed before BOD-277."""
        ladder = _ladder(tmp_path, rows, conns)
        req = TaskRequirements(coding=False)
        chosen, verdicts = ladder.select(req, now=NOW)
        ordered = sorted(
            [v for v in verdicts if v.rank is not None],
            key=lambda v: v.rank if v.rank is not None else 0,
        )
        return (chosen.route_id if chosen is not None else None, [v.route_id for v in ordered])

    def test_selection_identical_for_representative_fixture(self, tmp_path: Path) -> None:
        """Keep selection deterministic and expose ordered ranking and fallback details."""
        # A fixture that exercises capacity class, price, provider preference,
        # load, fit and route_id tiebreak simultaneously.
        rows = [
            _row("cc/claude-sonnet-5", owned_by="claude", pricing={"input": 3.0, "output": 15.0}),
            _row("cc/claude-haiku-5", owned_by="claude", pricing={"input": 0.25, "output": 1.25}),
            _row("cx/gpt-6-mini", owned_by="codex", pricing={"input": 0.15, "output": 0.6}),
        ]
        conns = [_conn("claude"), _conn("codex")]
        # 1) Selection is deterministic and reproducible.
        chosen1, order1 = self._pre_bod277_order(tmp_path, rows, conns)
        chosen2, order2 = self._pre_bod277_order(tmp_path / "b", rows, conns)
        assert chosen1 == chosen2
        assert order1 == order2
        # 2) Every candidate carries populated rank_components with the same
        # keys, in the same order, as the sort key (quota_percent displays quota_rank).
        ladder = _ladder(tmp_path / "c", rows, conns)
        _, verdicts = ladder.select(TaskRequirements(coding=False), now=NOW)
        candidates = [v for v in verdicts if v.rank_components is not None]
        assert candidates
        for v in candidates:
            assert v.rank_components is not None
            assert list(v.rank_components) == [  # ordered dict, rank keys first
                "capacity_order",
                "rating_basis",
                "rating",
                "declared_tier",
                "rating_metric",
                "rating_source",
                "rating_as_of",
                "slack",
                "quota_rank",
                "quota_percent",
                "expected_tokens",
                "provider_pref",
                "load",
                "fit",
                "route_id",
                "price",
                "probe_class",
                "evidence_level",
                "cache_checked_at",
                "cache_freshness",
                "session_score",
                "session_passes",
                "session_fails",
            ] + (
                ["fallback", "fallback_reason"]
                if v.reason == "selected" and v.capacity_class == CapacityClass.SUBSCRIPTION
                else []
            )

    def test_no_regression_from_test_orch_eligibility_case(self, tmp_path: Path) -> None:
        # Mirrors ``TestSelect::test_select_skips_unhealthy_and_picks_next``
        # from tests/test_orch_eligibility.py: with cc rate-limited the codex
        # route must still win, and the exposed data must match.
        rows = [
            _row("cc/claude-sonnet-5", owned_by="claude"),
            _row("cx/gpt-6-codex", owned_by="codex"),
        ]
        ladder = EligibilityLadder(
            inventory_rows=rows,
            connections=[_conn("claude"), _conn("codex")],
            probe=_Probe(
                {
                    "cc/claude-sonnet-5": HealthResult(
                        healthy=False, category="rate_limited", retry_after_seconds=42.0
                    )
                }
            ),
            state_path=tmp_path / "elig.json",
        )
        chosen, verdicts = ladder.select(TaskRequirements(), now=NOW)
        assert chosen is not None and chosen.route_id == "cx/gpt-6-codex"
        assert chosen.rank_components is not None
        failed = next(v for v in verdicts if v.route_id == "cc/claude-sonnet-5")
        # The route was ranked before probing (it entered the candidate pool)
        # and only failed HEALTHY when the probe returned rate_limited. Its
        # exposed rank_components reflect the truthful pre-probe evaluation;
        # explain_selection uses them to explain why the RUNNER-UP path lost.
        assert failed.failed_stage is EligibilityStage.HEALTHY
        assert failed.rank_components is not None


# ---------------------------------------------------------------------------
# 5. explain_selection() output
# ---------------------------------------------------------------------------


def _verdict(
    route_id: str,
    provider: str,
    reached: EligibilityStage | None,
    failed_stage: EligibilityStage | None,
    reason: str,
    *,
    rank: int | None = None,
    rc: Mapping[str, Any] | None = None,
) -> RouteVerdict:
    return RouteVerdict(
        route_id=route_id,
        provider=provider,
        reached=reached,
        failed_stage=failed_stage,
        reason=reason,
        rank=rank,
        rank_components={"capacity_order": 0, **rc} if rc is not None else None,
    )


class TestExplainSelection:
    def test_funnel_counts_are_cumulative(self) -> None:
        vs = [
            _verdict(
                "p/a",
                "p",
                EligibilityStage.SELECTED,
                None,
                "selected",
                rank=0,
                rc={
                    "capacity_order": 0,
                    "slack": 0,
                    "price": 1.0,
                    "provider_pref": 0,
                    "load": 0,
                    "fit": 2,
                    "route_id": "p/a",
                },
            ),
            _verdict(
                "p/b",
                "p",
                EligibilityStage.TASK_ELIGIBLE,
                None,
                "ranked",
                rank=1,
                rc={
                    "capacity_order": 0,
                    "slack": 0,
                    "price": 2.0,
                    "provider_pref": 0,
                    "load": 0,
                    "fit": 2,
                    "route_id": "p/b",
                },
            ),
            _verdict(
                "p/c", "p", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown:route"
            ),
            _verdict(
                "q/a", "q", EligibilityStage.ENTITLED, EligibilityStage.HEALTHY, "rate_limited"
            ),
            _verdict("r/a", "r", None, EligibilityStage.DISCOVERED, "excluded"),
        ]
        out = explain_selection(vs)
        f = out["funnel"]
        # 4 routes reached DISCOVERED (all but r/a which failed at DISCOVERED).
        assert f["DISCOVERED"] == 4
        assert f["ENTITLED"] == 4
        assert f["HEALTHY"] == 3  # p/a, p/b, p/c
        assert f["AVAILABLE"] == 2  # p/a, p/b
        assert f["TASK_ELIGIBLE"] == 2
        assert f["SELECTED"] == 1

    def test_rejections_grouped_by_stage_and_reason(self) -> None:
        vs = [
            _verdict(
                "p/a", "p", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown:route"
            ),
            _verdict(
                "p/b", "p", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown:route"
            ),
            _verdict(
                "q/a", "q", EligibilityStage.ENTITLED, EligibilityStage.HEALTHY, "rate_limited"
            ),
        ]
        rej = explain_selection(vs)["rejections"]
        assert rej == {"AVAILABLE": {"cooldown:route": 2}, "HEALTHY": {"rate_limited": 1}}

    def test_selected_because_reports_first_differing_component(self) -> None:
        rc_sel = {
            "rating_basis": "score",
            "rating": 70.0,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/sel",
            "capacity_order": 0,
            "price": 0.5,
        }
        rc_next = {
            "rating_basis": "score",
            "rating": 40.0,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/next",
            "capacity_order": 0,
            "price": 0.2,
        }
        vs = [
            _verdict("p/sel", "p", EligibilityStage.SELECTED, None, "selected", rank=0, rc=rc_sel),
            _verdict(
                "p/next", "p", EligibilityStage.TASK_ELIGIBLE, None, "ranked", rank=1, rc=rc_next
            ),
        ]
        out = explain_selection(vs)
        assert out["selected"]["route_id"] == "p/sel"
        assert out["runner_up"]["route_id"] == "p/next"
        reasons = out["selected_because"]
        # The rating decides; a cheaper, free runner-up is never the reason.
        assert reasons[0].startswith("equal on capacity_order")
        assert reasons[1].startswith("equal on rating_basis")
        assert "higher task rating than runner-up: 70.0 vs 40.0" in reasons[2]
        assert not any("price" in r for r in reasons)
        # Nothing after the deciding component.
        assert len(reasons) == 3

    def test_selected_because_names_intelligence_fallback_basis(self) -> None:
        rc_sel = {
            "rating_basis": "intelligence_fallback",
            "rating": 57.6,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/sel",
        }
        rc_next = {
            "rating_basis": "unknown",
            "rating": None,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/next",
        }
        vs = [
            _verdict("p/sel", "p", EligibilityStage.SELECTED, None, "selected", rank=0, rc=rc_sel),
            _verdict(
                "p/next", "p", EligibilityStage.TASK_ELIGIBLE, None, "ranked", rank=1, rc=rc_next
            ),
        ]
        reasons = explain_selection(vs)["selected_because"]
        assert reasons[1] == (
            "stronger rating evidence than runner-up: intelligence_fallback vs unknown"
        )

    def test_equal_fallback_bases_compare_their_own_metric_only(self) -> None:
        rc_sel = {
            "rating_basis": "intelligence_fallback",
            "rating": 57.6,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/sel",
        }
        rc_next = {
            "rating_basis": "intelligence_fallback",
            "rating": 40.0,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/next",
        }
        vs = [
            _verdict("p/sel", "p", EligibilityStage.SELECTED, None, "selected", rank=0, rc=rc_sel),
            _verdict(
                "p/next", "p", EligibilityStage.TASK_ELIGIBLE, None, "ranked", rank=1, rc=rc_next
            ),
        ]
        reasons = explain_selection(vs)["selected_because"]
        assert reasons[1].startswith("equal on rating_basis: intelligence_fallback")
        assert "higher task rating than runner-up: 57.6 vs 40.0" in reasons[2]

    def test_all_equal_or_unknown(self) -> None:
        rc_sel = {
            "rating_basis": "unknown",
            "rating": None,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/sel",
        }
        rc_next = {
            "rating_basis": "unknown",
            "rating": None,
            "declared_tier": None,
            "slack": 0,
            "quota_rank": 2,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/next",
        }
        vs = [
            _verdict("p/sel", "p", EligibilityStage.SELECTED, None, "selected", rank=0, rc=rc_sel),
            _verdict(
                "p/next", "p", EligibilityStage.TASK_ELIGIBLE, None, "ranked", rank=1, rc=rc_next
            ),
        ]
        reasons = explain_selection(vs)["selected_because"]
        # No component can decide; walk records honest state without inventing.
        assert any("rating unknown for both" in r for r in reasons)
        # route_id differs -> deciding component (tiebroken by route_id).
        assert reasons[-1].startswith("tiebroken by route_id: p/sel vs p/next")

    def test_no_selection_gives_empty_selected_because(self) -> None:
        vs = [
            _verdict(
                "p/a", "p", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown:route"
            )
        ]
        out = explain_selection(vs)
        assert out["selected"] is None
        assert out["runner_up"] is None
        assert out["selected_because"] == []

    def test_selected_without_runner_up_gives_empty_reasons(self) -> None:
        # Only the selected route has ranking data (single sufficient candidate).
        rc = {
            "capacity_order": 0,
            "slack": 0,
            "price": 1.0,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "p/only",
        }
        vs = [
            _verdict("p/only", "p", EligibilityStage.SELECTED, None, "selected", rank=0, rc=rc),
            _verdict(
                "q/x", "q", EligibilityStage.ENTITLED, EligibilityStage.HEALTHY, "rate_limited"
            ),
        ]
        out = explain_selection(vs)
        assert out["selected"]["route_id"] == "p/only"
        assert out["runner_up"] is None
        assert out["selected_because"] == []


# ---------------------------------------------------------------------------
# 6. Size bound: 7000-route event stays under 32 KB with candidates capped at 25
# ---------------------------------------------------------------------------


def test_event_size_bound_stays_under_32kb_with_25_candidates() -> None:
    import json

    verdicts = [
        RouteVerdict(
            route_id=f"provider-{i % 30}/model-{i}",
            provider=f"provider-{i % 30}",
            reached=EligibilityStage.HEALTHY,
            failed_stage=EligibilityStage.AVAILABLE,
            reason="cooldown",
        )
        for i in range(7000)
    ]
    # Add one ranked candidate with populated rank_components + capability data.
    verdicts.append(
        RouteVerdict(
            route_id="sel/best",
            provider="sel",
            reached=EligibilityStage.SELECTED,
            failed_stage=None,
            reason="selected",
            capacity_class=CapacityClass.SUBSCRIPTION,
            plan_label="max",
            rank=0,
            rank_components={
                "capacity_order": 0,
                "slack": 1,
                "price": None,
                "provider_pref": 0,
                "load": 0,
                "fit": 2,
                "route_id": "sel/best",
            },
            capability_tier=1,
            context_window=200_000,
            supports_tools=True,
            supports_structured_output=True,
            price=None,
        )
    )
    # PR #711 caps at 25; simulate that cap.
    sample = [v.to_dict() for v in verdicts[-25:]]
    blob = json.dumps(sample, default=str)
    assert len(blob.encode()) < 32_768


def test_rank_key_built_from_rank_components(tmp_path: Path) -> None:
    """_rank_components is the single source of truth for _rank_key (500 routes).

    The key is capacity tier, rating evidence, then slack, quota, provider
    preference, load, fit and route_id. Marginal price never enters it.
    """
    import random

    random.seed(42)
    rows = []
    for i in range(500):
        provider = random.choice(["anthropic", "openai", "google", "meta"])
        route_id = f"{provider}/model-{i:03d}"
        has_pricing = random.random() > 0.3  # 70% have pricing
        pricing = (
            {"input": random.uniform(0.5, 10.0), "output": random.uniform(1.0, 20.0)}
            if has_pricing
            else None
        )
        rows.append(
            _row(
                route_id,
                owned_by=provider,
                context=random.choice([8_000, 32_000, 128_000, 200_000]),
                tools=random.choice([True, False]),
                reasoning=random.choice([True, False]),
                pricing=pricing,
            )
        )

    conns = [
        _conn("anthropic", plan="max"),
        _conn("openai", plan="max"),
        _conn("google", plan="max"),
        _conn("meta", plan="max"),
    ]
    load_map = {row["id"]: random.randint(0, 5) for row in rows}
    ladder = EligibilityLadder(
        inventory_rows=rows,
        connections=conns,
        probe=_Probe(),
        state_path=tmp_path / "parity.json",
        prefer_providers=("anthropic", "openai"),
        max_probes_per_select=0,
        max_per_route=10,
        load=lambda rid: load_map.get(rid, 0),
    )
    # Synthetic ids carry no classifier tier, so accept any tier (unknown ranks last).
    requirements = TaskRequirements(
        max_capability_tier=3, min_context_tokens=16_000, required_capabilities=frozenset()
    )
    _assessments, candidates = ladder._assess_all(requirements, NOW)
    assert len(candidates) > 100
    for a in candidates:
        c = ladder._rank_components(a)
        assert ladder._rank_key(a) == (
            c["capacity_order"],
            int(c["expected_tokens"] is None) if a.capacity == CapacityClass.SUBSCRIPTION else 0,
            float(c["expected_tokens"] or 0) if a.capacity == CapacityClass.SUBSCRIPTION else 0.0,
            c["rating_rank"],
            -(c["rating"] or 0.0),
            c["declared_tier"] or 0,
            c["slack"],
            0 if a.capacity == CapacityClass.SUBSCRIPTION else c["quota_rank"],
            c["provider_pref"],
            c["load"],
            -c["fit"],
            c["route_id"],
        )
    # Changing only the price never moves a route.
    repriced = [{**row, "pricing": {"input": 0.0, "output": 0.0}} for row in rows]
    cheap = EligibilityLadder(
        inventory_rows=repriced,
        connections=conns,
        probe=_Probe(),
        state_path=tmp_path / "repriced.json",
        prefer_providers=("anthropic", "openai"),
        max_probes_per_select=0,
        max_per_route=10,
        load=lambda rid: load_map.get(rid, 0),
    )
    _, cheap_candidates = cheap._assess_all(requirements, NOW)
    assert [a.route_id for a in cheap_candidates] == [a.route_id for a in candidates]


def test_capacity_class_is_first_selection_reason() -> None:
    """Price excluded: a cheaper capacity class is never reported as the reason."""
    from verdict.orchestration.explain import _RANK_COMPONENT_ORDER

    assert _RANK_COMPONENT_ORDER[0] == "capacity_order"
    assert "price" not in _RANK_COMPONENT_ORDER
    assert _RANK_COMPONENT_ORDER[1:4] == ("rating_basis", "rating", "declared_tier")
