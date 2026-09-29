"""BOD-277 lane 2: routing_view projection from recorded eligibility evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import EligibilityStage, TaskRequirements
from verdict.orchestration.routing_view import (
    SCHEMA_VERSION,
    InventorySource,
    routing_view,
    routing_view_from_inventory,
)
from verdict.subagent_selection import HealthResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = "2026-09-28T12:00:00Z"


def _eligibility_event(
    *,
    seq: int = 1,
    node_id: str = "impl",
    selected: str | None = "kr/claude-opus",
    discovered: int = 10,
    entitled: int = 8,
    healthy: int = 6,
    available: int = 5,
    eligible: int = 3,
    candidates: list[dict[str, Any]] | None = None,
    candidates_omitted: int = 0,
    rejections: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "discovered": discovered,
        "entitled": entitled,
        "healthy": healthy,
        "available": available,
        "eligible": eligible,
        "selected": selected,
    }
    if candidates is not None:
        data["candidates"] = candidates
        data["candidates_omitted"] = candidates_omitted
    if rejections is not None:
        data["rejections"] = rejections
    return {"seq": seq, "at": _NOW, "type": "eligibility", "node_id": node_id, "data": data}


def _terminal_event(
    *,
    seq: int = 2,
    node_id: str = "impl",
    reported_model: str = "kr/claude-opus",
    session_ref: str = "sess-abc",
) -> dict[str, Any]:
    return {
        "seq": seq,
        "at": _NOW,
        "type": "terminal",
        "node_id": node_id,
        "data": {
            "ok": True,
            "route_id": reported_model,
            "reported_model": reported_model,
            "session_ref": session_ref,
        },
    }


def _candidate_dict(
    route_id: str = "kr/claude-opus",
    *,
    reached: str = "SELECTED",
    failed_stage: str | None = None,
    reason: str = "selected",
    rank: int = 0,
    capability_tier: int = 1,
    context_window: int = 200_000,
    supports_tools: bool = True,
    supports_structured_output: bool = True,
    capacity_class: str = "subscription",
    price: float | None = None,
    rank_components: dict[str, Any] | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "route_id": route_id,
        "provider": route_id.split("/")[0],
        "reached": reached,
        "failed_stage": failed_stage,
        "reason": reason,
        "rank": rank,
        "capability_tier": capability_tier,
        "context_window": context_window,
        "supports_tools": supports_tools,
        "supports_structured_output": supports_structured_output,
        "capacity_class": capacity_class,
        "plan_label": "max",
        "cooldown_until": None,
        "price": price,
    }
    if rank_components is not None:
        d["rank_components"] = rank_components
    return d


def _inv_row(
    route_id: str,
    *,
    context: int = 200_000,
    tools: bool = True,
    pricing: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    caps = {"tool_calling": tools, "structured_output": True, "reasoning": True}
    return {
        "id": route_id,
        "owned_by": route_id.split("/")[0],
        "context_length": context,
        "max_input_tokens": context,
        "capabilities": caps,
        "pricing": dict(pricing) if pricing is not None else {"input": 1.0, "output": 2.0},
    }


def _inv_conn(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": "oauth",
        "isActive": True,
        "testStatus": "ok",
        "backoffLevel": 0,
        "plan_label": "max",
        "rate_limited_until": None,
        "import_free_only": False,
    }


# ---------------------------------------------------------------------------
# 1. Funnel counts and reasons from a fixture eligibility event
# ---------------------------------------------------------------------------


class TestFunnelFromFixture:
    def test_funnel_counts_match_event(self) -> None:
        events = [_eligibility_event(discovered=10, entitled=8, healthy=6, available=5, eligible=3)]
        view = routing_view(events)
        assert len(view.evaluations) == 1
        e = view.evaluations[0]
        assert e.funnel["DISCOVERED"] == 10
        assert e.funnel["ENTITLED"] == 8
        assert e.funnel["HEALTHY"] == 6
        assert e.funnel["AVAILABLE"] == 5
        assert e.funnel["TASK_ELIGIBLE"] == 3

    def test_selected_route_set(self) -> None:
        events = [_eligibility_event(selected="kr/claude-opus")]
        view = routing_view(events)
        assert view.evaluations[0].selected_route == "kr/claude-opus"

    def test_no_selection_none(self) -> None:
        events = [_eligibility_event(selected=None)]
        view = routing_view(events)
        assert view.evaluations[0].selected_route is None

    def test_rejection_reasons_aggregated(self) -> None:
        rejections = {
            "HEALTHY": {"unhealthy": 2},
            "TASK_ELIGIBLE": {"context_too_small": 1, "tools_required": 1},
        }
        events = [_eligibility_event(rejections=rejections)]
        view = routing_view(events)
        e = view.evaluations[0]
        assert e.rejections["HEALTHY"] == {"unhealthy": 2}
        assert e.rejections["TASK_ELIGIBLE"]["context_too_small"] == 1

    def test_node_id_and_seq_preserved(self) -> None:
        events = [_eligibility_event(seq=42, node_id="review")]
        view = routing_view(events)
        e = view.evaluations[0]
        assert e.seq == 42
        assert e.node_id == "review"

    def test_schema_version(self) -> None:
        view = routing_view([_eligibility_event()])
        assert view.schema_version == SCHEMA_VERSION
        assert view.source == "recorded"


# ---------------------------------------------------------------------------
# 2. Unknown fields stay None
# ---------------------------------------------------------------------------


class TestUnknownFieldsNone:
    def test_price_none_when_absent(self) -> None:
        cand = _candidate_dict(price=None)
        events = [_eligibility_event(candidates=[cand])]
        view = routing_view(events)
        rec = view.evaluations[0].candidates[0]  # type: ignore[index]
        assert rec.price is None

    def test_capability_tier_none_when_absent(self) -> None:
        cand = _candidate_dict()
        del cand["capability_tier"]
        events = [_eligibility_event(candidates=[cand])]
        view = routing_view(events)
        rec = view.evaluations[0].candidates[0]  # type: ignore[index]
        assert rec.capability_tier is None

    def test_rank_components_none_when_absent(self) -> None:
        cand = _candidate_dict()
        # No rank_components key -> should be None
        events = [_eligibility_event(candidates=[cand])]
        view = routing_view(events)
        rec = view.evaluations[0].candidates[0]  # type: ignore[index]
        assert rec.rank_components is None

    def test_supports_tools_none_when_absent(self) -> None:
        cand = _candidate_dict()
        del cand["supports_tools"]
        events = [_eligibility_event(candidates=[cand])]
        view = routing_view(events)
        rec = view.evaluations[0].candidates[0]  # type: ignore[index]
        assert rec.supports_tools is None

    def test_context_window_none_when_absent(self) -> None:
        cand = _candidate_dict()
        del cand["context_window"]
        events = [_eligibility_event(candidates=[cand])]
        view = routing_view(events)
        rec = view.evaluations[0].candidates[0]  # type: ignore[index]
        assert rec.context_window is None


# ---------------------------------------------------------------------------
# 3. selected vs observed mismatch flagged
# ---------------------------------------------------------------------------


class TestMismatchFlag:
    def test_no_mismatch_when_same(self) -> None:
        events = [
            _eligibility_event(selected="kr/claude-opus"),
            _terminal_event(reported_model="kr/claude-opus"),
        ]
        view = routing_view(events)
        assert not view.evaluations[0].selected_observed_mismatch

    def test_mismatch_flagged_when_different(self) -> None:
        events = [
            _eligibility_event(selected="kr/claude-opus"),
            _terminal_event(reported_model="cc/claude-sonnet"),
        ]
        view = routing_view(events)
        assert view.evaluations[0].selected_observed_mismatch is True

    def test_retried_node_compares_each_evaluation_with_its_own_attempt(self) -> None:
        """A node that failed over must not show a mismatch against a later attempt."""
        events = [
            _eligibility_event(seq=1, selected="kr/claude-opus"),
            _terminal_event(seq=2, reported_model="kr/claude-opus"),
            _eligibility_event(seq=3, selected="cc/claude-sonnet"),
            _terminal_event(seq=4, reported_model="cc/claude-sonnet"),
        ]
        view = routing_view(events)
        first, second = view.evaluations
        assert first.observed_route == "kr/claude-opus"
        assert not first.selected_observed_mismatch
        assert second.observed_route == "cc/claude-sonnet"
        assert not second.selected_observed_mismatch

    def test_no_mismatch_when_no_terminal(self) -> None:
        events = [_eligibility_event(selected="kr/claude-opus")]
        view = routing_view(events)
        e = view.evaluations[0]
        assert e.observed_route is None
        assert not e.selected_observed_mismatch

    def test_observed_route_set_from_terminal(self) -> None:
        events = [
            _eligibility_event(selected="kr/claude-opus"),
            _terminal_event(reported_model="kr/claude-opus", session_ref="sess-xyz"),
        ]
        view = routing_view(events)
        e = view.evaluations[0]
        assert e.observed_route == "kr/claude-opus"
        assert e.session_ref == "sess-xyz"


# ---------------------------------------------------------------------------
# 4. Legacy events without candidates -> candidates None (not empty list)
# ---------------------------------------------------------------------------


class TestLegacyEventsNoCandidates:
    def test_legacy_event_candidates_none(self) -> None:
        """Events before PR #711 have no candidates key -> None, not []."""
        events = [_eligibility_event()]  # no candidates kwarg -> key absent
        view = routing_view(events)
        e = view.evaluations[0]
        assert e.candidates is None
        assert e.candidates_omitted == 0

    def test_modern_event_empty_candidates_list(self) -> None:
        """Explicit empty candidates list is valid - not the same as absent."""
        events = [_eligibility_event(candidates=[])]
        view = routing_view(events)
        e = view.evaluations[0]
        assert e.candidates == []

    def test_selected_because_empty_for_legacy(self) -> None:
        events = [_eligibility_event()]  # legacy
        view = routing_view(events)
        assert view.evaluations[0].selected_because == []


# ---------------------------------------------------------------------------
# 5. Inventory mode: real ladder on a small fixture inventory
# ---------------------------------------------------------------------------


class TestInventoryMode:
    def test_inventory_mode_returns_routing_view(self, tmp_path: Path) -> None:
        rows = [_inv_row("kr/claude-opus"), _inv_row("kr/claude-sonnet")]
        conns = [_inv_conn("kr")]
        view = routing_view_from_inventory(
            {},
            inventory=InventorySource(rows=rows, connections=conns),
            state_path=tmp_path / "elig.json",
        )
        assert view.source == "inventory"
        assert view.schema_version == SCHEMA_VERSION
        assert len(view.evaluations) == 1

    def test_inventory_mode_selection_matches_ladder(self, tmp_path: Path) -> None:
        """Inventory mode selection agrees with a direct EligibilityLadder.evaluate call."""
        from verdict.orchestration.eligibility import EligibilityLadder

        rows = [
            _inv_row("kr/claude-opus", pricing={"input": 1.0, "output": 2.0}),
            _inv_row("kr/claude-sonnet", pricing={"input": 0.5, "output": 1.0}),
        ]
        conns = [_inv_conn("kr")]

        def _null_probe(route_id: str) -> HealthResult:
            return HealthResult(healthy=True, category="")

        ladder = EligibilityLadder(
            inventory_rows=rows,
            connections=conns,
            probe=_null_probe,
            state_path=tmp_path / "elig.json",
        )
        verdicts = ladder.evaluate(TaskRequirements(), now=datetime.now(timezone.utc))
        direct_selected = next(
            (v.route_id for v in verdicts if v.reached == EligibilityStage.SELECTED), None
        )

        view = routing_view_from_inventory(
            {},
            inventory=InventorySource(rows=rows, connections=conns),
            state_path=tmp_path / "elig2.json",
        )
        view_selected = view.evaluations[0].selected_route
        assert view_selected == direct_selected

    def test_inventory_mode_funnel_nonzero(self, tmp_path: Path) -> None:
        rows = [_inv_row("kr/claude-opus")]
        conns = [_inv_conn("kr")]
        view = routing_view_from_inventory(
            {},
            inventory=InventorySource(rows=rows, connections=conns),
            state_path=tmp_path / "elig.json",
        )
        e = view.evaluations[0]
        assert e.funnel["DISCOVERED"] >= 1

    def test_probe_false_never_invokes_probe(self, tmp_path: Path) -> None:
        """With probe=False the probe callable is never invoked (evaluate path)."""
        from verdict.orchestration.eligibility import EligibilityLadder

        rows = [_inv_row("kr/claude-opus")]
        conns = [_inv_conn("kr")]

        def _bomb_probe(route_id: str) -> HealthResult:
            raise AssertionError("probe was called with probe=False")

        ladder = EligibilityLadder(
            inventory_rows=rows,
            connections=conns,
            probe=_bomb_probe,
            state_path=tmp_path / "elig.json",
        )
        # evaluate() must complete without calling _bomb_probe
        verdicts = ladder.evaluate(TaskRequirements(), now=datetime.now(timezone.utc))
        assert len(verdicts) >= 1

    def test_probe_false_candidates_show_unprobed(self, tmp_path: Path) -> None:
        """probe=False candidates report reached=ENTITLED and reason 'unprobed',
        never HEALTHY (health was not observed)."""
        rows = [_inv_row("kr/claude-opus"), _inv_row("kr/claude-sonnet")]
        conns = [_inv_conn("kr")]
        view = routing_view_from_inventory(
            {},
            inventory=InventorySource(rows=rows, connections=conns),
            state_path=tmp_path / "elig.json",
        )
        e = view.evaluations[0]
        # No route should be selected (no probing -> no route reaches SELECTED)
        assert e.selected_route is None
        # Every candidate that passed discovery+entitlement should show
        # reached=ENTITLED (stopped before HEALTHY) and reason=unprobed.
        for c in e.candidates:
            if c.reached is not None and c.reached not in ("DISCOVERED",):
                assert c.reached == "ENTITLED", (
                    f"{c.route_id} reached={c.reached}; expected ENTITLED without probing"
                )
                assert c.rejection_reason == "unprobed", (
                    f"{c.route_id} reason={c.rejection_reason}; expected 'unprobed'"
                )

    def test_probe_false_funnel_no_healthy(self, tmp_path: Path) -> None:
        """Without probing, no route reaches HEALTHY so the funnel count is 0."""
        rows = [_inv_row("kr/claude-opus")]
        conns = [_inv_conn("kr")]
        view = routing_view_from_inventory(
            {},
            inventory=InventorySource(rows=rows, connections=conns),
            state_path=tmp_path / "elig.json",
        )
        e = view.evaluations[0]
        assert e.funnel["HEALTHY"] == 0, (
            f"HEALTHY funnel count should be 0 without probing, got {e.funnel['HEALTHY']}"
        )


# ---------------------------------------------------------------------------
# 6. Deterministic JSON
# ---------------------------------------------------------------------------


class TestDeterministicJSON:
    def test_to_dict_schema_version_present(self) -> None:
        view = routing_view([_eligibility_event()])
        d = view.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION

    def test_to_json_sorted_keys(self) -> None:
        view = routing_view([_eligibility_event()])
        raw = view.to_json()
        parsed = json.loads(raw)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_candidates_omitted_count_preserved(self) -> None:
        cand = _candidate_dict()
        events = [_eligibility_event(candidates=[cand], candidates_omitted=7)]
        view = routing_view(events)
        assert view.evaluations[0].candidates_omitted == 7

    def test_funnel_keys_sorted_in_to_dict(self) -> None:
        events = [_eligibility_event()]
        view = routing_view(events)
        d = view.evaluations[0].to_dict()
        funnel_keys = list(d["funnel"].keys())
        assert funnel_keys == sorted(funnel_keys)

    def test_multiple_evaluations_ordered_by_event_order(self) -> None:
        events = [
            _eligibility_event(seq=1, node_id="impl"),
            _eligibility_event(seq=3, node_id="review"),
        ]
        view = routing_view(events)
        seqs = [e.seq for e in view.evaluations]
        assert seqs == [1, 3]
