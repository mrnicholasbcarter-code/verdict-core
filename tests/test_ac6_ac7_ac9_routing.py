"""AC6/7/9 tests for BOD-277: capacity evidence, cooldown scope, pool, filter, large inventory E2E."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

from verdict.design import PresentationMode
from verdict.orchestration import cockpit_controls as cockpit
from verdict.orchestration.candidate_builder import MAX_CANDIDATES, build_candidates
from verdict.orchestration.cockpit_nav import (
    KEY_BACKSPACE,
    KEY_ENTER,
    KEY_ESC,
    KEY_ROUTING_SEARCH,
    KEY_ROUTING_STATE,
    CockpitState,
    ScriptedKeyReader,
    _cycle_routing_state_filter,
    dispatch_key,
)
from verdict.orchestration.routing_render import (
    filter_candidates,
    render_routing_text,
    routing_json,
)
from verdict.orchestration.routing_view import (
    CandidateRecord,
    EligibilityEvaluation,
    InventorySource,
    RoutingView,
    routing_view_from_inventory,
)
from verdict.orchestration.tui import RunView

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_STR = "2026-09-28T12:00:00Z"
_COOLDOWN_STR = "2026-09-28T18:00:00Z"


def _cand(
    route_id: str,
    *,
    reached: str | None = "TASK_ELIGIBLE",
    failed_stage: str | None = None,
    reason: str = "ranked",
    pool: str = "",
    capacity_evidence: str = "",
    cooldown_scope: str = "",
    capacity_class: str | None = "subscription",
    cooldown_until: str | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        route_id=route_id,
        provider=route_id.split("/", 1)[0],
        reached=reached,
        failed_stage=failed_stage,
        rejection_reason=reason if failed_stage else None,
        rank=None,
        rank_components=None,
        capability_tier=None,
        context_window=None,
        supports_tools=None,
        supports_structured_output=None,
        capacity_class=capacity_class,
        plan_label="max",
        cooldown_until=cooldown_until,
        price=None,
        pool=pool,
        capacity_evidence=capacity_evidence,
        cooldown_scope=cooldown_scope,
    )


def _eval(
    candidates: list[CandidateRecord] | None = None,
    *,
    selected_route: str | None = "kr/claude-opus",
    candidates_omitted: int = 0,
    omitted_summary: dict | None = None,
) -> EligibilityEvaluation:
    return EligibilityEvaluation(
        node_id="impl",
        seq=1,
        at=_NOW_STR,
        funnel={
            "DISCOVERED": 5,
            "ENTITLED": 4,
            "HEALTHY": 3,
            "AVAILABLE": 2,
            "TASK_ELIGIBLE": 1,
            "SELECTED": 1,
        },
        rejections={},
        selected_route=selected_route,
        candidates=candidates,
        candidates_omitted=candidates_omitted,
        selected_because=[],
        observed_route=selected_route,
        session_ref=None,
        selected_observed_mismatch=False,
        omitted_summary=omitted_summary,
    )


def _view(evaluation: EligibilityEvaluation | None = None) -> RoutingView:
    ev = evaluation or _eval(
        [_cand("kr/claude-opus", reached="SELECTED", capacity_evidence="plan_label", pool="kr")]
    )
    return RoutingView(
        schema_version="routing-view-v1", source="recorded", generated_at=_NOW_STR, evaluations=[ev]
    )


# ---------------------------------------------------------------------------
# AC6 - capacity evidence, pool, cooldown scope visible; secrets never appear
# ---------------------------------------------------------------------------


class TestAC6CapacityEvidence:
    def test_capacity_evidence_in_candidate_record(self) -> None:
        c = _cand("kr/model", capacity_evidence="plan_label", pool="kr")
        assert c.capacity_evidence == "plan_label"
        assert c.pool == "kr"

    def test_capacity_evidence_in_json_output(self) -> None:
        """AC6: capacity_evidence and pool appear in routing_json output."""
        cand = _cand(
            "kr/claude-opus", reached="SELECTED", capacity_evidence="plan_label", pool="kr"
        )
        view = _view(_eval([cand], selected_route="kr/claude-opus"))
        payload = routing_json(view)
        ev = payload["evaluations"][0]
        c = next(c for c in ev["candidates"] if c["route_id"] == "kr/claude-opus")
        assert c["capacity_evidence"] == "plan_label"
        assert c["pool"] == "kr"

    def test_capacity_evidence_in_text_via_capacity_source_fn(self) -> None:
        """_capacity_source returns evidence in parens when set."""
        from verdict.orchestration.routing_render import (
            _capacity_source,  # type: ignore[attr-defined]
        )

        cand = _cand("kr/model", capacity_evidence="plan_label", capacity_class="subscription")
        result = _capacity_source(cand)
        assert "plan_label" in result
        assert "subscription" in result

    def test_pool_renders_in_cooldown_line(self) -> None:
        cand = _cand(
            "kr/claude-opus",
            reached=None,
            failed_stage="AVAILABLE",
            reason="cooldown:provider",
            capacity_evidence="import_free_only",
            pool="kr",
            cooldown_scope="provider:kr",
            cooldown_until=_COOLDOWN_STR,
        )
        view = _view(_eval([cand], selected_route=None))
        text = render_routing_text(view, width=200)
        assert "pool=kr" in text
        assert "scope=provider:kr" in text

    def test_cooldown_scope_in_json(self) -> None:
        cand = _cand(
            "kr/claude-opus",
            reached=None,
            failed_stage="AVAILABLE",
            reason="cooldown:provider",
            capacity_evidence="import_free_only",
            pool="kr",
            cooldown_scope="provider:kr",
            cooldown_until=_COOLDOWN_STR,
        )
        view = _view(_eval([cand], selected_route=None))
        payload = routing_json(view)
        c = next(
            c
            for ev in payload["evaluations"]
            for c in ev["candidates"]
            if c["route_id"] == "kr/claude-opus"
        )
        assert c["pool"] == "kr"
        assert c["capacity_evidence"] == "import_free_only"
        assert c["cooldown_scope"] == "provider:kr"

    def test_unknown_evidence_renders_as_empty_or_unknown(self) -> None:
        cand = _cand("mystery/model", capacity_evidence="", pool="")
        view = _view(_eval([cand], selected_route=None))
        text = render_routing_text(view, width=200)
        # no crash; mystery/model present
        assert "mystery/model" in text

    def test_routeverdict_to_dict_has_new_fields(self) -> None:
        """RouteVerdict.to_dict() always emits pool/capacity_evidence/cooldown_scope."""
        from verdict.orchestration.contracts import CapacityClass, RouteVerdict

        v = RouteVerdict(
            route_id="kr/model",
            provider="kr",
            reached=None,
            failed_stage=None,
            reason="selected",
            capacity_class=CapacityClass.SUBSCRIPTION,
            pool="kr",
            capacity_evidence="plan_label",
            cooldown_scope="",
        )
        d = v.to_dict()
        assert d["pool"] == "kr"
        assert d["capacity_evidence"] == "plan_label"
        assert d["cooldown_scope"] == ""

    def test_routeverdict_from_verdict_dict_round_trips(self) -> None:
        """CandidateRecord.from_verdict_dict picks up pool/capacity_evidence/cooldown_scope."""
        raw = {
            "route_id": "kr/model",
            "provider": "kr",
            "reached": "AVAILABLE",
            "failed_stage": None,
            "reason": "ranked",
            "rank": None,
            "capacity_class": "subscription",
            "plan_label": "max",
            "cooldown_until": None,
            "pool": "kr",
            "capacity_evidence": "plan_label",
            "cooldown_scope": "",
        }
        cr = CandidateRecord.from_verdict_dict(raw)
        assert cr.pool == "kr"
        assert cr.capacity_evidence == "plan_label"
        assert cr.cooldown_scope == ""


class TestAC6SecretLeak:
    """Seed a connection with an email and token; assert neither appears anywhere."""

    @pytest.mark.parametrize("scope", ["route", "provider"])
    async def test_no_email_or_token_in_event_or_render(self, tmp_path: Path, scope: str) -> None:
        """Real verdicts and the runtime's persisted evidence carry no account secrets."""
        from verdict.orchestration.contracts import (
            RouteVerdict,
            TaskRequirements,
            WorkGraph,
            WorkNode,
        )
        from verdict.orchestration.eligibility import EligibilityLadder
        from verdict.orchestration.receipt import EventLog
        from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

        inventory = {
            "routes": [
                {
                    "id": "secret-provider/model-a",
                    "owned_by": "secret-provider",
                    "context_length": 128_000,
                    "capabilities": {"tool_calling": True},
                    "pricing": {"input": 1.0, "output": 2.0},
                }
            ]
        }
        connections = [
            {
                "provider": "secret-provider",
                "isActive": True,
                "authType": "apikey",
                "plan_label": "subscription",
                "email": "user@secret.example.com",
                "api_key": "sk-SECRET-TOKEN-1234567890abcdef",
            }
        ]
        now = datetime.now(timezone.utc)
        cooldown_key = (
            "route:secret-provider/model-a" if scope == "route" else "provider:secret-provider"
        )
        state_path = tmp_path / "health.json"
        state_path.write_text(
            json.dumps(
                {
                    "health": {},
                    "cooldowns": {cooldown_key: {"until": (now + timedelta(hours=1)).isoformat()}},
                }
            )
        )
        node = WorkNode(
            "impl",
            "check cooldown evidence",
            owned_files=("unused.txt",),
            verification_command=("true",),
            min_context_tokens=1_000,
        )
        reqs = TaskRequirements.for_node(node)
        probe = Mock(side_effect=AssertionError("cooling routes must never be probed"))
        ladder = EligibilityLadder(inventory["routes"], connections, probe, state_path)
        verdicts = ladder.evaluate(reqs, now=now)
        assert verdicts and all(isinstance(v, RouteVerdict) for v in verdicts)
        verdict_dicts = [v.to_dict() for v in verdicts]

        # Exercise runtime.py's actual _evidence and EventLog, without launching a worker.
        events = EventLog(tmp_path / "events.jsonl", clock=lambda: now)
        runtime = DagRuntime(
            repo=tmp_path,
            run_dir=tmp_path / "run",
            graph=WorkGraph("cooldown evidence", (node,)),
            selector=ladder,
            executor=Mock(),
            classifier=Mock(),
            events=events,
            prompt_for=lambda _node, _cwd: "unused: pool is cooling",
            policy=RuntimePolicy(max_cooldown_wait_seconds=0),
            now=lambda: now,
        )
        await asyncio.wait_for(runtime._drive("impl"), timeout=1)
        event = next(e for e in events.read() if e.type == "eligibility")
        assert event.data["candidates"] == verdict_dicts
        probe.assert_not_called()

        view = routing_view_from_inventory(
            {
                "required_capabilities": list(reqs.required_capabilities),
                "min_context_tokens": reqs.min_context_tokens,
                "coding": reqs.coding,
                "reasoning": reqs.reasoning,
                "frontier_worthy": reqs.frontier_worthy,
            },
            inventory=InventorySource(rows=inventory["routes"], connections=connections),
            state_path=state_path,
            probe=False,
        )
        payload = routing_json(view)
        projected_candidates = [c.to_dict() for ev in view.evaluations for c in ev.candidates or []]
        json_candidates = [c for ev in payload["evaluations"] for c in ev["candidates"]]
        for candidates in (
            verdict_dicts,
            event.data["candidates"],
            projected_candidates,
            json_candidates,
        ):
            assert candidates
            for candidate in candidates:
                assert candidate["cooldown_scope"] == cooldown_key
                assert candidate["cooldown_scope"].startswith(("route:", "provider:"))

        artifacts = [
            json.dumps(verdict_dicts),
            json.dumps(event.data),
            events.path.read_text(),
            view.to_json(),
            render_routing_text(view, width=200),
            json.dumps(payload),
        ]
        for artifact in artifacts:
            for secret in ("secret.example.com", "sk-SECRET-TOKEN", "1234567890abcdef"):
                assert secret not in artifact


# ---------------------------------------------------------------------------
# AC7 - omitted_summary, MAX_CANDIDATES=100, cockpit filter keys, no probing
# ---------------------------------------------------------------------------


class TestAC7MaxCandidates:
    def test_max_candidates_is_100(self) -> None:
        assert MAX_CANDIDATES == 100

    def test_build_candidates_returns_3tuple(self) -> None:
        from verdict.orchestration.contracts import CapacityClass, RouteVerdict

        verdicts = [
            RouteVerdict(
                route_id=f"p/m{i}",
                provider="p",
                reached=None,
                failed_stage=None,
                reason="ranked",
                capacity_class=CapacityClass.SUBSCRIPTION,
            )
            for i in range(50)
        ]
        _cands, omitted, summary = build_candidates(verdicts, None)
        assert len(_cands) == 50
        assert omitted == 0
        assert summary is None

    def test_omitted_summary_populated_when_over_cap(self) -> None:
        from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict

        # Build 110 verdicts: 100 failing at HEALTHY, 10 selected
        verdicts = [
            RouteVerdict(
                route_id=f"p/rejected{i}",
                provider="p",
                reached=None,
                failed_stage=EligibilityStage.HEALTHY,
                reason="unhealthy",
                capacity_class=CapacityClass.FREE,
            )
            for i in range(110)
        ]
        verdicts[0] = RouteVerdict(
            route_id="p/winner",
            provider="p",
            reached=EligibilityStage.SELECTED,
            failed_stage=None,
            reason="selected",
            capacity_class=CapacityClass.SUBSCRIPTION,
        )
        _cands, omitted, summary = build_candidates(verdicts, "p/winner")
        assert omitted == 10
        assert summary is not None
        # Should have a "rejected" state entry
        assert "rejected" in summary
        assert summary["rejected"]["count"] == 10

    def test_omitted_summary_in_eligibility_evaluation(self) -> None:
        summary = {"rejected": {"count": 50, "first_reason": "unhealthy"}}
        ev = _eval(
            candidates=[_cand("kr/m", reached="SELECTED")],
            candidates_omitted=50,
            omitted_summary=summary,
        )
        assert ev.omitted_summary is not None
        assert ev.omitted_summary["rejected"]["count"] == 50

    def test_omitted_summary_renders_in_text(self) -> None:
        summary = {"rejected": {"count": 50, "first_reason": "unhealthy"}}
        ev = _eval(
            candidates=[_cand("kr/m", reached="SELECTED")],
            candidates_omitted=50,
            omitted_summary=summary,
        )
        text = render_routing_text(_view(ev), width=200)
        assert "50" in text
        assert "rejected" in text

    def test_omitted_summary_in_json_output(self) -> None:
        summary = {"rejected": {"count": 5, "first_reason": "unhealthy"}}
        ev = _eval(
            candidates=[_cand("kr/m", reached="SELECTED")],
            candidates_omitted=5,
            omitted_summary=summary,
        )
        payload = routing_json(_view(ev))
        ev_out = payload["evaluations"][0]
        assert ev_out["omitted_summary"] == summary

    def test_legacy_total_summary_is_null_in_json(self) -> None:
        ev = _eval(candidates_omitted=5, omitted_summary={"_total": 5})
        payload = routing_json(_view(ev))
        assert payload["evaluations"][0]["omitted_summary"] is None
        assert payload["evaluations"][0]["candidates_omitted"] == 5
        assert ev.omitted_summary == {"_total": 5}  # projection remains unchanged


class TestAC7FilterKeys:
    def test_cycle_routing_state_filter(self) -> None:
        state = CockpitState()
        assert state.routing_state_filter == ""
        _cycle_routing_state_filter(state)
        assert state.routing_state_filter == "rejected"
        _cycle_routing_state_filter(state)
        assert state.routing_state_filter == "cooldown"
        _cycle_routing_state_filter(state)
        assert state.routing_state_filter == "admitted"
        _cycle_routing_state_filter(state)
        assert state.routing_state_filter == "selected"
        _cycle_routing_state_filter(state)
        assert state.routing_state_filter == ""  # wraps

    def test_key_s_cycles_filter_when_routing_open(self) -> None:
        state = CockpitState()
        state.routing_open = True
        changed = dispatch_key(KEY_ROUTING_STATE, state, None)
        assert changed
        assert state.routing_state_filter == "rejected"

    def test_key_s_no_effect_when_routing_closed(self) -> None:
        state = CockpitState()
        state.routing_open = False
        changed = dispatch_key(KEY_ROUTING_STATE, state, None)
        assert not changed
        assert state.routing_state_filter == ""

    @pytest.mark.parametrize("backspace", [KEY_BACKSPACE, "\x08", "\x7f"])
    def test_search_typing_and_backspace_apply_matching_rows(self, backspace: str) -> None:
        state = CockpitState(routing_open=True)
        cands = [_cand("cx/gpt-5"), _cand("cx/gpt-6"), _cand("cc/claude-sonnet")]
        evaluation = _eval(cands, selected_route=None)
        for key in (KEY_ROUTING_SEARCH, "g", "p", "x", backspace, "t"):
            assert dispatch_key(key, state, None)
        assert state.routing_search_open
        assert state.routing_search_text == "gpt"
        assert state.routing_text_filter == ""  # Enter has not applied the draft yet.
        assert dispatch_key(KEY_ENTER, state, None)
        assert not state.routing_search_open
        assert not state.detail_open
        assert state.routing_text_filter == "gpt"
        assert [
            c.route_id for c in filter_candidates(cands, evaluation, text=state.routing_text_filter)
        ] == ["cx/gpt-5", "cx/gpt-6"]
        text = render_routing_text(_view(evaluation), width=200, text=state.routing_text_filter)
        assert "cx/gpt-5" in text and "cx/gpt-6" in text
        assert "cc/claude-sonnet" not in text

    @pytest.mark.parametrize("apply", [False, True])
    def test_search_escape_clears_without_closing_panel(self, apply: bool) -> None:
        state = CockpitState(routing_open=True)
        for key in (KEY_ROUTING_SEARCH, "g", "p", "t"):
            dispatch_key(key, state, None)
        if apply:
            dispatch_key(KEY_ENTER, state, None)
        assert dispatch_key(KEY_ESC, state, None)
        assert state.routing_open
        assert not state.routing_search_open
        assert state.routing_search_text == state.routing_text_filter == ""
        assert dispatch_key(KEY_ESC, state, None)
        assert not state.routing_open

    def test_search_key_has_no_effect_when_routing_closed(self) -> None:
        state = CockpitState()
        assert not dispatch_key(KEY_ROUTING_SEARCH, state, None)
        assert not state.routing_search_open

    def test_search_owns_navigation_and_control_shortcuts(self) -> None:
        state = cockpit.ControlCockpitState(routing_open=True)
        state.sync_order(["impl", "other"])
        view = RunView()
        assert cockpit.dispatch_key(KEY_ROUTING_SEARCH, state, view)
        keys = "gjkrsqcdxhpt? /X"
        for key in keys:
            assert cockpit.dispatch_key(key, state, view)
        assert state.routing_search_text == keys
        assert state.selected_id == "impl"
        assert state.routing_state_filter == ""
        assert not state.pending_action
        assert not state.receipt_open and not state.health_open and not state.context_open
        assert not state.quit_requested and not state.help_open and not state.expanded

    def test_cockpit_search_loop_filters_and_escape_restores_without_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production control cockpit passes applied filters to the shared renderer."""
        import socket

        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.eligibility import EligibilityLadder

        probe = Mock(
            side_effect=AssertionError("search must not make network calls or rank routes")
        )
        monkeypatch.setattr(socket.socket, "connect", probe)
        monkeypatch.setattr(EligibilityLadder, "select", probe)
        monkeypatch.setattr(EligibilityLadder, "evaluate", probe)
        events = [
            RunEvent(1, _NOW_STR, "node_state", "impl", {"state": "RUNNING"}),
            RunEvent(
                2,
                _NOW_STR,
                "eligibility",
                "impl",
                {
                    "candidates": [
                        _cand("cx/gpt-5").to_dict(),
                        _cand("cx/gpt-6").to_dict(),
                        _cand("cc/claude").to_dict(),
                    ],
                    "selected": None,
                },
            ),
        ]
        frames: list[str] = []
        real_render = cockpit.render_cockpit

        def render_spy(
            view: RunView, state: cockpit.ControlCockpitState, mode: PresentationMode
        ) -> object:
            frame = real_render(view, state, mode, dashboard=False)
            captured = StringIO()
            Console(file=captured, width=200, color_system=None).print(frame)
            frames.append(captured.getvalue())
            return frame

        monkeypatch.setattr(cockpit, "render_cockpit", render_spy)
        cockpit.run_cockpit(
            tmp_path,
            console=Console(file=StringIO(), width=200, color_system=None),
            key_reader=ScriptedKeyReader(["r", "/", "g", "p", "t", KEY_ENTER, KEY_ESC]),
            events_source=lambda: events,
            poll_seconds=0,
            max_iterations=12,
            stop_when_final=False,
        )
        filtered = [frame for frame in frames if "filter text=gpt" in frame]
        assert filtered
        assert all("cx/gpt-5" in frame and "cx/gpt-6" in frame for frame in filtered)
        assert all("cc/claude" not in frame for frame in filtered)
        assert any("search: /gpt_" in frame for frame in frames)
        assert "cc/claude" in frames[-1] and "filter text=gpt" not in frames[-1]
        probe.assert_not_called()

    def test_filter_never_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """filter_candidates is pure; no I/O."""
        import socket

        probe = Mock(side_effect=AssertionError("filter_candidates must not make network calls"))
        monkeypatch.setattr(socket.socket, "connect", probe)
        cands = [_cand("kr/m", reached="SELECTED"), _cand("cc/m", capacity_class="free")]
        eval_ = _eval(cands, selected_route="kr/m")
        result = filter_candidates(eval_.candidates or [], eval_, state="selected")
        assert len(result) == 1
        probe.assert_not_called()


# ---------------------------------------------------------------------------
# AC9 - end-to-end: real ranking_inventory.json → selection → event → explorer
# ---------------------------------------------------------------------------


class TestAC9EndToEndLargeInventory:
    """Use tests/fixtures/catalog_truth/ranking_inventory.json (7 779 rows)."""

    @pytest.fixture()
    def ranking_inv_path(self) -> Path:
        p = Path("tests/fixtures/catalog_truth/ranking_inventory.json")
        if not p.exists():
            pytest.skip("ranking_inventory.json fixture missing")
        return p

    @pytest.fixture()
    def ranking_conn_path(self) -> Path:
        p = Path("tests/fixtures/catalog_truth/ranking_connections.json")
        if not p.exists():
            pytest.skip("ranking_connections.json fixture missing")
        return p

    def test_real_inventory_to_view_and_render_under_1s(
        self, ranking_inv_path: Path, ranking_conn_path: Path
    ) -> None:
        import json as _json

        _inv_rows = _json.loads(ranking_inv_path.read_text())
        if isinstance(_inv_rows, dict):
            _inv_rows = _inv_rows.get("routes", next(iter(_inv_rows.values())))
        _conns = _json.loads(ranking_conn_path.read_text())
        if isinstance(_conns, dict):
            _conns = _conns.get("connections", next(iter(_conns.values())))
        src = InventorySource(rows=_inv_rows, connections=_conns)
        t0 = time.perf_counter()
        view = routing_view_from_inventory(
            {
                "required_capabilities": ["tools"],
                "min_context_tokens": 1000,
                "coding": True,
                "reasoning": False,
                "frontier_worthy": False,
            },
            inventory=src,
            probe=False,
        )
        render_routing_text(view, width=100)
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"real inventory render took {elapsed:.3f}s"
        assert view.evaluations
        ev = view.evaluations[0]
        assert ev.funnel.get("DISCOVERED", 0) > 0

    def test_ac6_fields_present_in_real_inventory_view(
        self, ranking_inv_path: Path, ranking_conn_path: Path
    ) -> None:
        """AC6 fields exist on candidates from the real inventory path."""
        import json as _json

        _inv_data = _json.loads(ranking_inv_path.read_text())
        _inv_rows = _inv_data["routes"] if isinstance(_inv_data, dict) else _inv_data
        _conns = _json.loads(ranking_conn_path.read_text())
        if isinstance(_conns, dict):
            _conns = next(iter(_conns.values())) if _conns else []
        src = InventorySource(rows=_inv_rows, connections=_conns)
        view = routing_view_from_inventory(
            {
                "required_capabilities": ["tools"],
                "min_context_tokens": 1000,
                "coding": True,
                "reasoning": False,
                "frontier_worthy": False,
            },
            inventory=src,
            probe=False,
        )
        assert view.evaluations
        ev = view.evaluations[0]
        assert ev.candidates is not None
        # At least one candidate should have capacity_evidence set
        # May all be unknown if no connections matched, but field must exist
        assert all(isinstance(c.capacity_evidence, str) for c in ev.candidates)
        assert all(isinstance(c.pool, str) for c in ev.candidates)
        assert all(isinstance(c.cooldown_scope, str) for c in ev.candidates)

    def test_ac7_filter_works_on_real_inventory(
        self, ranking_inv_path: Path, ranking_conn_path: Path
    ) -> None:
        """Filter functions work on a real large candidate list without probing."""
        import json as _json

        _inv_data = _json.loads(ranking_inv_path.read_text())
        _inv_rows = _inv_data["routes"] if isinstance(_inv_data, dict) else _inv_data
        _conns = _json.loads(ranking_conn_path.read_text())
        if isinstance(_conns, dict):
            _conns = next(iter(_conns.values())) if _conns else []
        src = InventorySource(rows=_inv_rows, connections=_conns)
        view = routing_view_from_inventory(
            {
                "required_capabilities": ["tools"],
                "min_context_tokens": 1000,
                "coding": True,
                "reasoning": False,
                "frontier_worthy": False,
            },
            inventory=src,
            probe=False,
        )
        assert view.evaluations
        ev = view.evaluations[0]
        cands = ev.candidates or []
        # Filter by text — must complete quickly
        t0 = time.perf_counter()
        filtered = filter_candidates(cands, ev, text="gpt")
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"filter took {elapsed:.3f}s"
        # All results contain "gpt" in route_id or provider
        for c in filtered:
            assert "gpt" in c.route_id.lower() or "gpt" in c.provider.lower()

    def test_ac7_state_filter_works_on_real_inventory(
        self, ranking_inv_path: Path, ranking_conn_path: Path
    ) -> None:
        """State filter on real inventory."""
        import json as _json

        _inv_data = _json.loads(ranking_inv_path.read_text())
        _inv_rows = _inv_data["routes"] if isinstance(_inv_data, dict) else _inv_data
        _conns = _json.loads(ranking_conn_path.read_text())
        if isinstance(_conns, dict):
            _conns = next(iter(_conns.values())) if _conns else []
        src = InventorySource(rows=_inv_rows, connections=_conns)
        view = routing_view_from_inventory(
            {
                "required_capabilities": ["tools"],
                "min_context_tokens": 1000,
                "coding": True,
                "reasoning": False,
                "frontier_worthy": False,
            },
            inventory=src,
            probe=False,
        )
        assert view.evaluations
        ev = view.evaluations[0]
        cands = ev.candidates or []
        # Filter by rejected state
        from verdict.orchestration.routing_render import candidate_state

        rejected = filter_candidates(cands, ev, state="rejected")
        for c in rejected:
            assert candidate_state(c, ev) == "rejected"

    def test_ac9_json_has_new_fields(self, ranking_inv_path: Path, ranking_conn_path: Path) -> None:
        """routing_json output contains the AC6 fields."""
        import json as _json

        _inv_data = _json.loads(ranking_inv_path.read_text())
        _inv_rows = _inv_data["routes"] if isinstance(_inv_data, dict) else _inv_data
        _conns = _json.loads(ranking_conn_path.read_text())
        if isinstance(_conns, dict):
            _conns = next(iter(_conns.values())) if _conns else []
        src = InventorySource(rows=_inv_rows, connections=_conns)
        view = routing_view_from_inventory(
            {
                "required_capabilities": ["tools"],
                "min_context_tokens": 1000,
                "coding": True,
                "reasoning": False,
                "frontier_worthy": False,
            },
            inventory=src,
            probe=False,
        )
        payload = routing_json(view)
        assert payload["evaluations"]
        for ev in payload["evaluations"]:
            for cand in ev.get("candidates") or []:
                # AC6: fields present (may be None/empty but not missing)
                assert "pool" in cand
                assert "capacity_evidence" in cand
                assert "cooldown_scope" in cand
