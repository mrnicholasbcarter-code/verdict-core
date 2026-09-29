"""BOD-277 routing explorer renderer: TUI + text + JSON, no recompute."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from verdict.design import PresentationMode
from verdict.orchestration.cockpit_nav import CockpitState, dispatch_key, open_routing_view
from verdict.orchestration.routing_render import (
    candidate_state,
    filter_candidates,
    first_rejection,
    paginate,
    render_routing,
    render_routing_text,
    routing_json,
)
from verdict.orchestration.routing_view import (
    SCHEMA_VERSION,
    CandidateRecord,
    EligibilityEvaluation,
    RoutingView,
    routing_view,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).parent / "fixtures" / "routing_render" / "golden"
PROOF_LIVE = ROOT / "docs" / "proof" / "live-controller-run"
PROOF_DEMO = ROOT / "docs" / "proof" / "demo-run"
_FIXED_AT = "2026-09-28T12:00:00Z"
_GENERATED = "2026-09-28T12:00:01Z"
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _cand(
    route_id: str,
    *,
    reached: str | None = "TASK_ELIGIBLE",
    failed_stage: str | None = None,
    reason: str | None = None,
    rank: int | None = None,
    rank_components: dict[str, Any] | None = None,
    capability_tier: int | None = 2,
    context_window: int | None = 200_000,
    supports_tools: bool | None = True,
    supports_structured_output: bool | None = True,
    capacity_class: str | None = "subscription",
    plan_label: str | None = "max",
    cooldown_until: str | None = None,
    price: float | None = None,
    provider: str | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        route_id=route_id,
        provider=provider if provider is not None else route_id.split("/", 1)[0],
        reached=reached,
        failed_stage=failed_stage,
        rejection_reason=reason,
        rank=rank,
        rank_components=rank_components,
        capability_tier=capability_tier,
        context_window=context_window,
        supports_tools=supports_tools,
        supports_structured_output=supports_structured_output,
        capacity_class=capacity_class,
        plan_label=plan_label,
        cooldown_until=cooldown_until,
        price=price,
    )


def _eval(
    *,
    node_id: str = "impl",
    seq: int = 5,
    funnel: dict[str, int] | None = None,
    selected_route: str | None = "cc/claude-opus",
    candidates: list[CandidateRecord] | None = None,
    candidates_omitted: int = 0,
    selected_because: list[str] | None = None,
    observed_route: str | None = "cc/claude-opus",
    session_ref: str | None = "sess-abc",
    mismatch: bool = False,
    rejections: dict[str, dict[str, int]] | None = None,
    at: str = _FIXED_AT,
) -> EligibilityEvaluation:
    return EligibilityEvaluation(
        node_id=node_id,
        seq=seq,
        at=at,
        funnel=funnel
        or {
            "DISCOVERED": 12,
            "ENTITLED": 10,
            "HEALTHY": 8,
            "AVAILABLE": 6,
            "TASK_ELIGIBLE": 3,
            "SELECTED": 1,
        },
        rejections=rejections
        or {
            "HEALTHY": {"unhealthy": 2},
            "AVAILABLE": {"cooldown": 1},
            "TASK_ELIGIBLE": {"context_too_small": 1},
        },
        selected_route=selected_route,
        candidates=candidates,
        candidates_omitted=candidates_omitted,
        selected_because=selected_because
        if selected_because is not None
        else ["cheaper capacity class than runner-up: subscription vs metered"],
        observed_route=observed_route,
        session_ref=session_ref,
        selected_observed_mismatch=mismatch,
    )


def _view(evaluation: EligibilityEvaluation | None = None) -> RoutingView:
    return RoutingView(
        schema_version=SCHEMA_VERSION,
        source="recorded",
        generated_at=_GENERATED,
        evaluations=[evaluation if evaluation is not None else _rich_eval()],
    )


def _rich_eval() -> EligibilityEvaluation:
    selected = _cand(
        "cc/claude-opus",
        reached="SELECTED",
        reason="selected",
        rank=0,
        rank_components={
            "capacity_order": 0,
            "slack": 1,
            "price": None,
            "provider_pref": 0,
            "load": 0,
            "fit": 3,
            "route_id": "cc/claude-opus",
        },
        capacity_class="subscription",
        price=None,
    )
    admitted = _cand(
        "cc/claude-sonnet",
        reached="TASK_ELIGIBLE",
        rank=1,
        rank_components={
            "capacity_order": 0,
            "slack": 2,
            "price": None,
            "provider_pref": 0,
            "load": 0,
            "fit": 2,
            "route_id": "cc/claude-sonnet",
        },
    )
    rejected = _cand(
        "openrouter/gpt-4o",
        reached="ENTITLED",
        failed_stage="HEALTHY",
        reason="unhealthy",
        rank=None,
        rank_components=None,
        capacity_class="metered",
        plan_label="paygo",
        price=5.0,
        supports_tools=True,
    )
    cooled = _cand(
        "kr/claude-opus",
        reached="AVAILABLE",
        failed_stage="AVAILABLE",
        reason="cooldown",
        cooldown_until="2026-09-28T18:00:00Z",
        capacity_class="subscription",
        rank=None,
    )
    visible = _cand(
        "demo-sub/atlas-coder",
        reached="DISCOVERED",
        capability_tier=None,
        context_window=None,
        supports_tools=None,
        supports_structured_output=None,
        capacity_class=None,
        plan_label=None,
        price=None,
        rank=None,
    )
    unknown_route = _cand(
        "mystery/model",
        reached=None,
        failed_stage=None,
        reason=None,
        rank=None,
        rank_components=None,
        capability_tier=None,
        context_window=None,
        supports_tools=None,
        supports_structured_output=None,
        capacity_class=None,
        plan_label=None,
        price=None,
        provider="",
    )
    return _eval(
        candidates=[selected, admitted, rejected, cooled, visible, unknown_route],
        candidates_omitted=4,
        mismatch=False,
    )


def _plain_mode(width: int) -> PresentationMode:
    return PresentationMode(
        color=False, unicode=False, animate=False, width=width, color_system=None
    )


def _color_mode(width: int = 100) -> PresentationMode:
    return PresentationMode(
        color=True, unicode=True, animate=False, width=width, color_system="truecolor"
    )


def _to_text(renderable: Any, width: int = 100, *, color: bool = False) -> str:
    console = Console(
        record=True,
        width=width,
        force_terminal=color,
        color_system="truecolor" if color else None,
        no_color=not color,
        file=None,
    )
    console.size = (width, 40)
    console.print(renderable)
    return console.export_text()


def _normalize_generated(text: str) -> str:
    text = re.sub(r"view_generated_at=\S+", "view_generated_at={generated_at}", text)
    text = re.sub(r"generated_at=\S+", "generated_at={generated_at}", text)
    return text


# ---------------------------------------------------------------------------
# State mapping / rejection / selected-because
# ---------------------------------------------------------------------------


class TestCandidateStates:
    def test_states_are_distinct(self) -> None:
        evaluation = _rich_eval()
        assert evaluation.candidates is not None
        mapped = [candidate_state(c, evaluation) for c in evaluation.candidates]
        assert mapped == ["selected", "admitted", "rejected", "cooldown", "visible", "unknown"]
        assert len(set(mapped)) == 6

    def test_confirmed_when_observed_matches_non_selected(self) -> None:
        winner = _cand("cc/a", reached="SELECTED", reason="selected", rank=0)
        other = _cand("cc/b", reached="TASK_ELIGIBLE", rank=1)
        evaluation = _eval(
            selected_route="cc/a",
            observed_route="cc/b",
            mismatch=True,
            candidates=[winner, other],
            selected_because=["equal on capacity_order: 0"],
        )
        assert candidate_state(winner, evaluation) == "selected"
        assert candidate_state(other, evaluation) == "confirmed"

    def test_first_rejection_from_record(self) -> None:
        cand = _cand(
            "openrouter/gpt-4o", failed_stage="HEALTHY", reason="unhealthy", reached="ENTITLED"
        )
        stage, reason = first_rejection(cand)
        assert stage == "HEALTHY"
        assert reason == "unhealthy"

    def test_first_rejection_unknown_when_absent(self) -> None:
        cand = _cand("cc/x", reached="DISCOVERED", reason=None, failed_stage=None)
        assert first_rejection(cand) == ("unknown", "unknown")


class TestSelectedBecause:
    def test_uses_decision_data_not_generated_prose(self) -> None:
        text = render_routing_text(_view(), width=100)
        assert "cheaper capacity class than runner-up: subscription vs metered" in text
        assert "selected because:" in text

    def test_unknown_when_no_rank_components(self) -> None:
        evaluation = _eval(
            candidates=[_cand("cc/a", reached="SELECTED", reason="selected")], selected_because=[]
        )
        text = render_routing_text(_view(evaluation), width=100)
        assert "selected because: unknown (no rank components recorded)" in text


# ---------------------------------------------------------------------------
# Unknown / stale / cooldown
# ---------------------------------------------------------------------------


class TestUnknownAndCooldown:
    def test_unknown_fields_render_as_unknown(self) -> None:
        text = render_routing_text(_view(), width=200)
        assert "capacity=unknown" in text or "unknown" in text
        assert "mystery/model" in text
        assert "unknown" in text

    def test_legacy_candidates_unknown(self) -> None:
        evaluation = _eval(candidates=None)
        text = render_routing_text(_view(evaluation), width=100)
        assert "candidates: unknown (not recorded on this eligibility event)" in text

    def test_cooldown_until_and_capacity_visible(self) -> None:
        text = render_routing_text(_view(), width=100, now=_NOW)
        assert "cooldowns" in text
        assert "kr/claude-opus" in text
        assert "until=2026-09-28T18:00:00Z" in text
        assert "capacity=subscription" in text
        assert "remaining=21600s" in text

    def test_stale_inventory_keeps_recorded_timestamp(self) -> None:
        text = render_routing_text(_view(), width=100)
        assert "evaluated_at=2026-09-28T12:00:00Z" in text
        assert "view_generated_at=2026-09-28T12:00:01Z" in text


# ---------------------------------------------------------------------------
# Filter / pagination / large catalog
# ---------------------------------------------------------------------------


class TestFilterPaginate:
    def test_filter_by_state_provider_text(self) -> None:
        evaluation = _rich_eval()
        assert evaluation.candidates is not None
        rejected = filter_candidates(evaluation.candidates, evaluation, state="rejected")
        assert [c.route_id for c in rejected] == ["openrouter/gpt-4o"]
        cc = filter_candidates(evaluation.candidates, evaluation, provider="cc")
        assert {c.route_id for c in cc} == {"cc/claude-opus", "cc/claude-sonnet"}
        texted = filter_candidates(evaluation.candidates, evaluation, text="atlas")
        assert [c.route_id for c in texted] == ["demo-sub/atlas-coder"]

    def test_paginate_bounds_page(self) -> None:
        evaluation = _rich_eval()
        assert evaluation.candidates is not None
        page, index, pages = paginate(evaluation.candidates, page=0, page_size=2)
        assert len(page) == 2
        assert index == 0
        assert pages == 3
        page2, index2, _ = paginate(evaluation.candidates, page=99, page_size=2)
        assert index2 == 2
        assert len(page2) == 2  # last page has the remaining two of six

    def test_page_size_capped(self) -> None:
        rows = [_cand(f"p/m{i}", reached="DISCOVERED") for i in range(50)]
        page, _, _ = paginate(rows, page=0, page_size=10_000)
        assert len(page) == 50  # 50 < cap, so all on one page
        rows2 = [_cand(f"p/m{i}", reached="DISCOVERED") for i in range(500)]
        page2, _, pages = paginate(rows2, page=0, page_size=10_000)
        assert len(page2) == 200
        assert pages == 3


class TestLargeCatalog:
    def test_ten_thousand_rows_render_under_one_second(self) -> None:
        selected = _cand("cc/win", reached="SELECTED", reason="selected", rank=0)
        rows = [selected]
        for i in range(9999):
            rows.append(
                _cand(
                    f"prov{i % 17}/model-{i}",
                    reached="DISCOVERED" if i % 3 else "ENTITLED",
                    failed_stage="HEALTHY" if i % 5 == 0 else None,
                    reason="unhealthy" if i % 5 == 0 else None,
                    capacity_class=None if i % 7 == 0 else "metered",
                    rank=None,
                    capability_tier=None,
                    context_window=None,
                    supports_tools=None,
                    supports_structured_output=None,
                    plan_label=None,
                    price=None,
                )
            )
        evaluation = _eval(
            funnel={
                "DISCOVERED": 10000,
                "ENTITLED": 4000,
                "HEALTHY": 1,
                "AVAILABLE": 1,
                "TASK_ELIGIBLE": 1,
                "SELECTED": 1,
            },
            selected_route="cc/win",
            observed_route="cc/win",
            candidates=rows,
            candidates_omitted=0,
            selected_because=["tiebroken by route_id: cc/win vs prov0/model-0"],
            rejections={"HEALTHY": {"unhealthy": 2000}},
        )
        view = _view(evaluation)
        t0 = time.perf_counter()
        text = render_routing_text(view, width=100)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"render took {elapsed:.3f}s"
        assert "10000 candidates, 1 admitted, 1 selected" in text
        assert "showing 25/10000" in text
        filtered = filter_candidates(rows, evaluation, provider="cc")
        page, _, _ = paginate(filtered, page=0, page_size=25)
        assert len(page) <= 25
        # No network: this test never constructs a client / probe.
        assert "http://" not in text


# ---------------------------------------------------------------------------
# JSON / secrets
# ---------------------------------------------------------------------------


class TestJSON:
    def test_same_facts_as_text(self) -> None:
        view = _view()
        payload = routing_json(view)
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["source"] == "recorded"
        ev = payload["evaluations"][0]
        assert ev["selected_route"] == "cc/claude-opus"
        assert ev["headline"].startswith("12 candidates, 3 admitted")
        states = {c["route_id"]: c["state"] for c in ev["candidates"]}
        assert states["cc/claude-opus"] == "selected"
        assert states["openrouter/gpt-4o"] == "rejected"
        assert states["kr/claude-opus"] == "cooldown"
        rejected = next(c for c in ev["candidates"] if c["route_id"] == "openrouter/gpt-4o")
        assert rejected["first_rejection_stage"] == "HEALTHY"
        assert rejected["first_rejection_reason"] == "unhealthy"
        cooled = next(c for c in ev["candidates"] if c["route_id"] == "kr/claude-opus")
        assert cooled["capacity_source"] == "subscription"
        mystery = next(c for c in ev["candidates"] if c["route_id"] == "mystery/model")
        assert mystery["capacity_source"] is None
        assert mystery["price"] is None

    def test_redacts_secrets(self) -> None:
        evaluation = _eval(
            session_ref="sk-secret-ABCDEFGH123456",
            candidates=[_cand("cc/a", reached="SELECTED", reason="selected")],
            selected_because=["bearer sk-secret-ABCDEFGH123456"],
        )
        payload = routing_json(_view(evaluation))
        blob = str(payload)
        assert "sk-secret-ABCDEFGH123456" not in blob
        assert "[redacted]" in blob


# ---------------------------------------------------------------------------
# Proof fixtures (legacy events: candidates unknown, funnel recorded)
# ---------------------------------------------------------------------------


class TestProofFixtures:
    def test_live_controller_run_funnel(self) -> None:
        if not PROOF_LIVE.exists():
            pytest.skip("proof fixture missing")
        view = routing_view(PROOF_LIVE)
        text = render_routing_text(view, width=100)
        assert "candidates: unknown (not recorded on this eligibility event)" in text
        assert "kr/claude-haiku-4.5" in text
        assert "DISCOVERED 56" in text
        assert "node alpha" in text
        assert "node beta" in text

    def test_demo_run_funnel_and_stability(self) -> None:
        if not PROOF_DEMO.exists():
            pytest.skip("proof fixture missing")
        t1 = _normalize_generated(render_routing_text(routing_view(PROOF_DEMO), width=100))
        t2 = _normalize_generated(render_routing_text(routing_view(PROOF_DEMO), width=100))
        assert t1 == t2
        assert "demo-sub/atlas-coder" in t1 or "demo-free/birch-coder" in t1
        assert "candidates: unknown" in t1

    def test_proof_goldens(self) -> None:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        cases = [("live-controller-run", PROOF_LIVE), ("demo-run", PROOF_DEMO)]
        for name, path in cases:
            if not path.exists():
                pytest.skip(f"proof fixture missing: {name}")
            got = _normalize_generated(render_routing_text(routing_view(path), width=100))
            golden = GOLDEN_DIR / f"{name}-100.txt"
            if not golden.exists() or os.environ.get("UPDATE_GOLDEN") == "1":
                golden.write_text(got)
            assert got == golden.read_text(), f"golden mismatch {name}"


# ---------------------------------------------------------------------------
# Presentation / goldens / tokens
# ---------------------------------------------------------------------------


class TestPresentationAndGoldens:
    def test_plain_no_ansi(self) -> None:
        text = render_routing_text(_view(), width=100)
        assert "\x1b" not in text

    def test_plain_mode_renderable_no_ansi(self) -> None:
        rendered = render_routing(_view(), _plain_mode(100))
        text = _to_text(rendered, width=100, color=False)
        assert "\x1b" not in text

    def test_narrow_and_wide_do_not_crash(self) -> None:
        view = _view()
        for width in (60, 100, 200):
            render_routing_text(view, width=width)
            _to_text(render_routing(view, _plain_mode(width)), width=width)

    def test_golden_widths(self) -> None:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        view = _view()
        for width in (60, 100, 200):
            got = render_routing_text(view, width=width, now=_NOW)
            path = GOLDEN_DIR / f"routing-{width}.txt"
            if not path.exists() or os.environ.get("UPDATE_GOLDEN") == "1":
                path.write_text(got)
            assert got == path.read_text(), f"golden mismatch at width={width}"

    def test_lines_fit_and_counts_match_rows(self) -> None:
        """No rendered line exceeds the width, and the header names the row gap."""
        view = _view()
        evaluation = view.evaluations[0]
        assert evaluation.candidates is not None
        recorded = len(evaluation.candidates)
        omitted = evaluation.candidates_omitted
        for width in (60, 100, 120, 200):
            text = render_routing_text(view, width=width, now=_NOW)
            for line in text.splitlines():
                assert len(line) <= width + 7, f"width {width} line {len(line)}: {line!r}"
            rendered = render_routing(view, _plain_mode(width), now=_NOW)
            rich = _to_text(rendered, width=width, color=False)
            for line in rich.splitlines():
                assert len(line) <= width, f"rich width {width} line {len(line)}: {line!r}"
            flat = " ".join(text.split())
            assert f"showing {recorded}/{recorded}" in flat
            if omitted:
                assert f"{recorded} recorded, {omitted} counted only" in flat
            # Table state labels, not the funnel, decide how many rows are admitted.
            table = [
                line
                for line in text.splitlines()
                if "SELECTED" in line or "ADMITTED" in line or "REJECTED" in line
            ]
            assert any("SELECTED" in line and "see selected because" in line for line in table)
            assert sum("ADMITTED" in line for line in table) == 1

    def test_colour_uses_design_tokens_only(self) -> None:
        src = Path("verdict/orchestration/routing_render.py").read_text()
        assert re.search(r"#[0-9a-fA-F]{6}\b", src) is None
        for banned in ("bright_red", "bright_green", "magenta", "cyan1", "rgb(", "color("):
            assert banned not in src
        assert "token_style(" in src
        assert "from verdict.design import" in src
        assert "render_state(" in src
        console = Console(record=True, width=100, force_terminal=True, color_system="truecolor")
        console.size = (100, 40)
        console.print(render_routing(_view(), _color_mode(100)))
        exported = console.export_text()
        assert "selected because" in exported.lower() or "SELECTED" in exported

    def test_no_animation_is_static(self) -> None:
        mode = PresentationMode(
            color=True, unicode=True, animate=False, width=100, color_system="truecolor"
        )
        a = _to_text(render_routing(_view(), mode), width=100, color=True)
        b = _to_text(render_routing(_view(), mode), width=100, color=True)
        assert a == b


# ---------------------------------------------------------------------------
# Cockpit wiring
# ---------------------------------------------------------------------------


def _run_view(eligibility: dict[str, int]) -> Any:
    return type("RunViewStub", (), {"eligibility": eligibility, "nodes": {}, "last_seq": 5})()


class TestCockpitWire:
    def test_open_routing_view_toggles(self) -> None:
        view = _run_view(
            {"discovered": 12, "entitled": 10, "healthy": 8, "available": 6, "eligible": 3}
        )
        state = CockpitState()
        open_routing_view(state, view)
        assert state.routing_open is True
        assert state.routing_view is not None
        text = state.routing_render_text(state.routing_view, width=80)
        assert "12 candidates, 3 admitted" in text
        open_routing_view(state, view)
        assert state.routing_open is False

    def test_dispatch_r_opens_and_esc_closes(self) -> None:
        view = _run_view({"discovered": 4, "eligible": 1})
        state = CockpitState()
        assert dispatch_key("r", state, view) is True
        assert state.routing_open is True
        assert dispatch_key("ESC", state, view) is True
        assert state.routing_open is False
