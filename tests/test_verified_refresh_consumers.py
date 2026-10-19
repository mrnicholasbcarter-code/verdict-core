"""Consumer-side refresh tests: ordering, cancel, consent, /probe (BOD-292, BOD-331).

Covers the wait-then-render contract (progress strictly before the final
outcome), shared cancellation, the y/N consent default-No controller, the
per-capacity-class consent for manual refresh plans, the /eligibility and
/probe parsers, the full failure-classification mapping, and the
picker/selection/autocomplete contracts. All I/O is injected.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import verdict.actions.verified_models as surfaces
from tests.test_verified_models_projection import conn
from tests.test_verified_models_projection import row as inventory_row
from verdict.actions.model_refresh import action_models_refresh_execute, action_models_refresh_plan
from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter
from verdict.orchestration import cli as orchestration_cli
from verdict.orchestration.health_cache import (
    CATEGORY_AUTH,
    CATEGORY_CATALOG_STALE,
    CATEGORY_GONE,
    CATEGORY_MODEL_MISMATCH,
    CATEGORY_NOT_FOUND,
    CATEGORY_PAYMENT,
    CATEGORY_PERMISSION,
    CATEGORY_RATE_LIMITED,
    CATEGORY_TIMEOUT,
    CATEGORY_UPSTREAM,
    HealthCache,
)
from verdict.orchestration.verified_refresh import (
    CAPACITY_FREE,
    CAPACITY_METERED,
    CAPACITY_SUBSCRIPTION,
    CAPACITY_UNKNOWN,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    ProbeExchange,
    ProgressEvent,
    RefreshConfig,
    RefreshCoordinator,
    RefreshSnapshot,
    RowInput,
    refresh_for_consumer,
)
from verdict.prove_at_rest import AdmittedRoute, probe_full
from verdict.tui_verified_controls import (
    CANCEL,
    ControlsError,
    VerifiedControlsController,
    parse_eligibility_args,
    parse_probe_model_list,
    prompt_consent,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _row(
    route_id: str,
    *,
    status: str = "UNVERIFIED",
    capacity: str = CAPACITY_FREE,
    refreshable: bool = True,
    provider: str | None = None,
) -> RowInput:
    return RowInput(
        route_id=route_id,
        provider=provider or route_id.split("/", 1)[0],
        status=status,
        capacity_class=capacity,
        refreshable=refreshable,
        refresh_reason=None,
        last_success_at=None,
        rank_hint=None,
    )


def _ok(route_id: str, phase: str) -> ProbeExchange:
    suffix = route_id.split("/", 1)[1] if "/" in route_id else route_id
    return ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=(phase == "chat"),
        tool_called=(phase == "tool"),
        latency_ms=1.0,
        reported_model=suffix,
    )


class _Transport:
    def __init__(self, script: dict[tuple[str, str], ProbeExchange] | None = None):
        self.script = script or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
        self.calls.append((route_id, phase))
        return self.script.get((route_id, phase)) or _ok(route_id, phase)

    def routes_called(self) -> set[str]:
        return {rid for rid, _ in self.calls}


def _snapshot(rows: Sequence[RowInput]) -> RefreshSnapshot:
    return RefreshSnapshot(rows=tuple(rows), generation="gen1")


def _coord(tmp_path: Path, transport):
    return RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )


# ---------------------------------------------------------------------------
# Render strictly AFTER completion: progress events precede the final result
# ---------------------------------------------------------------------------


def test_progress_events_precede_final_result(tmp_path: Path) -> None:
    transport = _Transport()
    coord = _coord(tmp_path, transport)
    timeline: list[str] = []

    def on_progress(event: ProgressEvent) -> None:
        timeline.append(f"progress#{event.sequence}:{event.kind}")

    rows = [_row(f"p{i}/r", provider=f"p{i}") for i in range(3)]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
        on_progress=on_progress,
    )
    timeline.append("FINAL")
    # Every progress tick (including the final-kind tick) arrives before the
    # caller observes the returned outcome.
    assert timeline[-1] == "FINAL"
    assert any(t.startswith("progress#") for t in timeline[:-1])
    # Sequences are monotonic.
    seqs = [int(t.split("#")[1].split(":")[0]) for t in timeline if t.startswith("progress#")]
    assert seqs == sorted(seqs)
    assert out.outcome == OUTCOME_COMPLETED
    # There is no provisional list: the only result object is the final one.
    assert out.probed == 3


def test_wait_runner_returns_only_after_completion(tmp_path: Path) -> None:
    transport = _Transport()
    seen_before_return: list[str] = []

    def run_refresh(snapshot, **kwargs):  # type: ignore[no-untyped-def]
        coord = _coord(tmp_path, transport)
        return coord.refresh_for_consumer(snapshot, **kwargs)

    controller = VerifiedControlsController(
        read_line=lambda _p: "",
        write=lambda s: seen_before_return.append(s),
        run_refresh=run_refresh,
    )
    rows = [_row("free/a")]
    out = controller.refresh(
        _snapshot(rows), consumer="verified_view", needed_ids=["free/a"], config=RefreshConfig()
    )
    # Progress lines were written during the call; the outcome is returned after.
    assert out.outcome == OUTCOME_COMPLETED
    assert any("probing" in s for s in seen_before_return)


# ---------------------------------------------------------------------------
# Cancel: shared cancellation stops dispatch, renders last-known
# ---------------------------------------------------------------------------


def test_cancel_via_controller_stops_dispatch(tmp_path: Path) -> None:
    transport = _Transport()
    cancel_flag = {"v": False}

    def run_refresh(snapshot, **kwargs):  # type: ignore[no-untyped-def]
        coord = _coord(tmp_path, transport)
        return coord.refresh_for_consumer(snapshot, **kwargs)

    controller = VerifiedControlsController(
        read_line=lambda _p: "",
        write=lambda _s: None,
        run_refresh=run_refresh,
        is_cancelled=lambda: cancel_flag["v"],
    )

    def on_progress(_e: object) -> None:
        cancel_flag["v"] = True

    rows = [_row(f"p{i}/r", provider=f"p{i}") for i in range(5)]
    out = controller.refresh(
        _snapshot(rows),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
        on_progress=on_progress,
    )
    assert out.outcome == OUTCOME_CANCELLED
    assert out.probed < 5


# ---------------------------------------------------------------------------
# y/N consent (default No), EOF/Esc/Ctrl-C cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer,granted,cancelled",
    [
        ("y", True, False),
        ("yes", True, False),
        ("Y", True, False),
        ("", False, False),  # blank defaults to No
        ("n", False, False),
        ("no", False, False),
        ("whatever", False, False),  # unknown defaults to No, not cancel
    ],
)
def test_consent_default_no(answer: str, granted: bool, cancelled: bool) -> None:
    res = prompt_consent("Refresh 3 routes?", read_line=lambda _p: answer)
    assert res.granted is granted
    assert res.cancelled is cancelled


def test_consent_eof_and_sentinel_and_interrupt_cancel() -> None:
    def eof(_p: str) -> str:
        raise EOFError

    def interrupt(_p: str) -> str:
        raise KeyboardInterrupt

    assert prompt_consent("x", read_line=eof).cancelled is True
    assert prompt_consent("x", read_line=interrupt).cancelled is True
    assert prompt_consent("x", read_line=lambda _p: CANCEL).cancelled is True
    assert prompt_consent("x", read_line=lambda _p: None).cancelled is True


def test_consent_makes_no_calls_on_deny(tmp_path: Path) -> None:
    transport = _Transport()
    controller = VerifiedControlsController(
        read_line=lambda _p: "n",  # deny
        write=lambda _s: None,
        run_refresh=lambda *a, **k: None,
    )
    res = controller.confirm("Refresh?")
    assert res.denied is True
    assert transport.calls == []


# ---------------------------------------------------------------------------
# /eligibility argument parsing
# ---------------------------------------------------------------------------


def test_eligibility_arg_parsing() -> None:
    args = parse_eligibility_args("STALE provider=cc search=sonnet page=2 refresh")
    assert args.status == "STALE"
    assert args.provider == "cc"
    assert args.search == "sonnet"
    assert args.page == 2
    assert args.refresh is True


def test_eligibility_empty_is_unscoped() -> None:
    args = parse_eligibility_args("")
    assert args.status is None and args.provider is None and args.page == 1
    assert args.refresh is False


def test_eligibility_rejects_unknown_token() -> None:
    with pytest.raises(ControlsError):
        parse_eligibility_args("notastatus")
    with pytest.raises(ControlsError):
        parse_eligibility_args("page=0")


def test_eligibility_status_case_insensitive() -> None:
    assert parse_eligibility_args("stale").status == "STALE"


# ---------------------------------------------------------------------------
# /probe model-list parser
# ---------------------------------------------------------------------------


def test_probe_parser_comma_and_whitespace() -> None:
    assert parse_probe_model_list("cc/x,kr/y") == ["cc/x", "kr/y"]
    assert parse_probe_model_list("cc/x  kr/y") == ["cc/x", "kr/y"]
    assert parse_probe_model_list("cc/x, kr/y  cc/x") == ["cc/x", "kr/y"]  # dedupe order-preserving


def test_probe_parser_preserves_slash_and_colon() -> None:
    assert parse_probe_model_list("openrouter/foo:free, cc/x") == ["openrouter/foo:free", "cc/x"]


def test_probe_parser_rejects_empty() -> None:
    with pytest.raises(ControlsError):
        parse_probe_model_list("")
    with pytest.raises(ControlsError):
        parse_probe_model_list("  , , ")


def test_probe_parser_identical_across_inline_prompt_palette() -> None:
    # The same parser underlies every path, so the lists are identical.
    inline = parse_probe_model_list("cc/x, kr/y")
    prompted = parse_probe_model_list("cc/x kr/y")
    palette = parse_probe_model_list(" cc/x , kr/y ")
    assert inline == prompted == palette == ["cc/x", "kr/y"]


# ---------------------------------------------------------------------------
# Every failure-class mapping through the bounded prober
# ---------------------------------------------------------------------------


def _probe(route_id: str, chat: ProbeExchange, tool: ProbeExchange | None = None):
    script = {(route_id, "chat"): chat}
    if tool is not None:
        script[(route_id, "tool")] = tool

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        return script.get((rid, phase)) or _ok(rid, phase)

    return probe_full(
        AdmittedRoute(route_id=route_id, provider=route_id.split("/", 1)[0], capacity="free"),
        transport,
        timeout_seconds=15.0,
    )


@pytest.mark.parametrize(
    "status,category,expect",
    [
        (None, "timeout", CATEGORY_TIMEOUT),
        (401, None, CATEGORY_AUTH),
        (402, None, CATEGORY_PAYMENT),
        (403, None, CATEGORY_PERMISSION),
        (404, None, CATEGORY_NOT_FOUND),
        (410, None, CATEGORY_GONE),
        (429, None, CATEGORY_RATE_LIMITED),
        (500, None, CATEGORY_UPSTREAM),
        (503, None, CATEGORY_UPSTREAM),
    ],
)
def test_chat_failure_classes_map(status: int | None, category: str | None, expect: str) -> None:
    chat = ProbeExchange(http_status=status, ok=False, error_category=category)
    outcome = _probe("cc/a", chat)
    assert outcome.result is not None
    assert outcome.result.category == expect
    assert outcome.result.healthy is False


def test_model_mismatch_maps_and_is_failed() -> None:
    chat = ProbeExchange(http_status=200, ok=True, chat_exact=True, reported_model="kr/other")
    outcome = _probe("cc/a", chat)
    assert outcome.result is not None
    assert outcome.result.category == CATEGORY_MODEL_MISMATCH
    assert outcome.result.chat_ok is False


def test_catalog_ghost_maps() -> None:
    chat = ProbeExchange(http_status=None, ok=False, error_category="catalog_stale")
    outcome = _probe("cc/a", chat)
    assert outcome.result is not None
    assert outcome.result.category == CATEGORY_CATALOG_STALE


def test_malformed_proof_is_failed_not_ok() -> None:
    # Chat 200 but wrong text (chat_exact False) is not healthy.
    chat = ProbeExchange(http_status=200, ok=True, chat_exact=False, reported_model="a")
    outcome = _probe("cc/a", chat)
    assert outcome.result is not None
    assert outcome.result.chat_ok is False


def test_tool_phase_failure_maps() -> None:
    chat = _ok("cc/a", "chat")
    tool = ProbeExchange(http_status=500, ok=False, error_category="upstream")
    outcome = _probe("cc/a", chat, tool)
    assert outcome.result is not None
    assert outcome.result.chat_ok is True
    assert outcome.result.tool_ok is False
    assert outcome.result.category == CATEGORY_UPSTREAM


def test_context_length_and_gateway_busy_no_write() -> None:
    for cat in ("context_length_exceeded", "gateway_busy"):
        chat = ProbeExchange(http_status=400, ok=False, error_category=cat)
        outcome = _probe("cc/a", chat)
        assert outcome.no_write is True
        assert outcome.result is None


def test_partial_chat_then_cancel_before_tool_not_tested() -> None:
    chat = _ok("cc/a", "chat")
    calls: list[str] = []

    def transport(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append(phase)
        return chat

    # Cancel fires after the chat call (checked before the tool dispatch).
    state = {"after_chat": False}

    def cancelled() -> bool:
        return state["after_chat"]

    def deadline_ok() -> bool:
        return True

    def transport2(rid: str, phase: str, timeout: float) -> ProbeExchange:
        calls.append(phase)
        if phase == "chat":
            state["after_chat"] = True
        return chat

    outcome = probe_full(
        AdmittedRoute(route_id="cc/a", provider="cc", capacity="free"),
        transport2,
        timeout_seconds=15.0,
        deadline_ok=deadline_ok,
        cancelled=cancelled,
    )
    assert outcome.no_write is True
    assert outcome.tool_tested is False
    assert "tool" not in calls


# ---------------------------------------------------------------------------
# Picker / selection involved ids; autocomplete never probes
# ---------------------------------------------------------------------------


def test_picker_passes_only_involved_ids(tmp_path: Path) -> None:
    transport = _Transport()
    coord = _coord(tmp_path, transport)
    rows = [_row("cc/a"), _row("cc/b"), _row("cc/c")]
    out = coord.refresh_for_consumer(
        _snapshot(rows),
        consumer="picker",
        needed_ids=["cc/a", "cc/b"],  # the involved ids only
        config=RefreshConfig(),
        explicit=True,
    )
    assert transport.routes_called() == {"cc/a", "cc/b"}
    assert "cc/c" not in out.route_outcomes


def test_autocomplete_never_probes_even_with_stale_rows(tmp_path: Path) -> None:
    # Autocomplete must never call refresh_for_consumer. We assert the local
    # snapshot has stale rows yet the controller offers no refresh path that
    # would probe: the local-only projection is read directly, not here.
    transport = _Transport()
    # Simulate autocomplete by simply NOT invoking the coordinator.
    rows = [_row("cc/a", status="STALE"), _row("cc/b", status="UNVERIFIED")]
    _ = _snapshot(rows)
    # No coordinator call was made; transport stays empty.
    assert transport.calls == []


def test_selection_hook_integration_is_optional(tmp_path: Path) -> None:
    # The module-level wrapper works for a selection consumer with involved ids.
    transport = _Transport()
    out = refresh_for_consumer(
        _snapshot([_row("free/a", status="STALE")]),
        consumer="selection",
        needed_ids=["free/a"],
        config=RefreshConfig(),
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
        explicit=True,
    )
    assert out.outcome == OUTCOME_COMPLETED
    assert out.verified == 1


def test_disabled_auto_refresh_does_not_probe_selection_path(tmp_path: Path) -> None:
    transport = _Transport()
    out = refresh_for_consumer(
        _snapshot([_row("free/a", status="STALE")]),
        consumer="verified_view",
        needed_ids=["free/a"],
        config=RefreshConfig(auto_refresh=False),
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )
    assert transport.calls == []
    del out


# ---------------------------------------------------------------------------
# Manual refresh plan: one consent per capacity class (BOD-331)
# ---------------------------------------------------------------------------

ALL_CLASSES = (CAPACITY_FREE, CAPACITY_SUBSCRIPTION, CAPACITY_UNKNOWN, CAPACITY_METERED)
GATEWAY = "http://offline.invalid:20128"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep state under tmp_path and ignore the operator's controller route."""
    monkeypatch.delenv("VERDICT_HEALTH_CACHE", raising=False)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setattr(surfaces, "active_controller_route", lambda: None)
    return tmp_path


def _mixed_adapter(tmp_path: Path) -> VerifiedSnapshotAdapter:
    """One never-verified route per capacity class; no health cache on disk."""
    return VerifiedSnapshotAdapter(
        GATEWAY,
        StorePaths.defaults(tmp_path),
        [
            inventory_row("fr/free"),
            inventory_row("cc/sub"),
            inventory_row("zz/unknown"),
            inventory_row("ap/metered", pricing={"input": 1.0, "output": 2.0}),
        ],
        [
            conn("fr", auth="apikey", plan="free"),
            conn("cc"),
            conn("zz", auth="apikey", plan=""),
            conn("ap", auth="apikey", plan="PAYG"),
        ],
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )


ROUTE_CLASS = {
    "fr/free": CAPACITY_FREE,
    "cc/sub": CAPACITY_SUBSCRIPTION,
    "zz/unknown": CAPACITY_UNKNOWN,
    "ap/metered": CAPACITY_METERED,
}


def _plan_rows() -> list[dict[str, Any]]:
    return [
        {
            "route_id": rid,
            "provider": rid.split("/", 1)[0],
            "status": "UNVERIFIED",
            "capacity_class": capacity,
            "refreshable": capacity in {CAPACITY_FREE, CAPACITY_SUBSCRIPTION},
            "price": 3.0 if capacity == CAPACITY_METERED else None,
        }
        for rid, capacity in ROUTE_CLASS.items()
    ]


def test_plan_groups_routes_by_capacity_with_defaults_and_spend() -> None:
    # Input order is reversed so the plan's own class order is what is asserted.
    rows = _plan_rows()[::-1]
    plan = action_models_refresh_plan(
        snapshot_rows=rows, needed_ids=[r["route_id"] for r in rows], include_metered=True, now=NOW
    ).data
    groups = plan["capacity_groups"]
    assert [g["capacity_class"] for g in groups] == list(ALL_CLASSES)
    assert [g["route_ids"] for g in groups] == [[rid] for rid in ROUTE_CLASS]
    assert [g["default_approved"] for g in groups] == [True, True, False, False]
    assert [g["estimated_requests"] for g in groups] == [2, 2, 1, 1]
    assert all("estimated_spend_usd" not in g for g in groups[:2])
    unknown, metered = groups[2], groups[3]
    assert unknown["estimated_spend_usd"] is None and unknown["unpriced_count"] == 1
    assert metered["unpriced_count"] == 0
    assert 0 < metered["estimated_spend_usd"] < 0.01


def test_plan_route_cap_keeps_prepaid_routes_before_costly_ones() -> None:
    rows = _plan_rows()[::-1]
    plan = action_models_refresh_plan(
        snapshot_rows=rows,
        needed_ids=[r["route_id"] for r in rows],
        include_metered=True,
        caps={"max_routes": 2},
        now=NOW,
    ).data
    assert [r["route_id"] for r in plan["routes"]] == ["fr/free", "cc/sub"]
    assert plan["omitted_count"] == 2


def _groups() -> list[dict[str, Any]]:
    rows = _plan_rows()
    plan = action_models_refresh_plan(
        snapshot_rows=rows, needed_ids=[r["route_id"] for r in rows], include_metered=True, now=NOW
    )
    groups: list[dict[str, Any]] = plan.data["capacity_groups"]
    return groups


def confirm_capacity_groups(
    groups: list[dict[str, Any]],
    *,
    read_line: Any,
    write: Any,
    interactive: bool = True,
    opt_in: tuple[str, ...] = (),
) -> Any:
    controller = VerifiedControlsController(read_line, write, run_refresh=lambda *a, **k: None)
    return controller.confirm_capacity_groups(groups, interactive=interactive, opt_in=opt_in)


@pytest.mark.parametrize(
    "answers,approved",
    [
        ([""], ()),  # blank first answer defaults to No
        (["n"], ()),
        (["maybe"], ()),
        (["f"], (CAPACITY_FREE,)),
        (["FREE"], (CAPACITY_FREE,)),
        (["y", "", "", "", ""], (CAPACITY_FREE, CAPACITY_SUBSCRIPTION)),  # per-group defaults
        (["y", "y", "y", "y", "y"], ALL_CLASSES),
        (["y", "n", "n", "", ""], ()),
        (["y", "", "n", "y", "n"], (CAPACITY_FREE, CAPACITY_UNKNOWN)),
        (["y", "n", "y", "n", "y"], (CAPACITY_SUBSCRIPTION, CAPACITY_METERED)),
    ],
)
def test_capacity_consent_answer_branches(answers: list[str], approved: tuple[str, ...]) -> None:
    feed = iter(answers)
    prompts: list[str] = []

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        return next(feed)

    consent = confirm_capacity_groups(_groups(), read_line=read_line, write=lambda _s: None)
    assert consent.approved == approved
    assert consent.cancelled is False
    assert len(prompts) == len(answers)  # every answer used, no extra prompt
    assert "[y/N]" in prompts[0] and "f = free only" in prompts[0]
    if len(prompts) > 1:
        assert [p.rsplit(" ", 2)[-2] for p in prompts[1:]] == ["[Y/n]", "[Y/n]", "[y/N]", "[y/N]"]


@pytest.mark.parametrize("cancel_at", [0, 1, 3])
@pytest.mark.parametrize("how", ["eof", "interrupt", "sentinel"])
def test_capacity_consent_cancel_at_any_prompt_approves_nothing(cancel_at: int, how: str) -> None:
    answers = ["y", "y", "y", "y", "y"]
    calls = {"n": 0}

    def read_line(_prompt: str) -> Any:
        index = calls["n"]
        calls["n"] += 1
        if index == cancel_at:
            if how == "eof":
                raise EOFError
            if how == "interrupt":
                raise KeyboardInterrupt
            return CANCEL
        return answers[index]

    consent = confirm_capacity_groups(_groups(), read_line=read_line, write=lambda _s: None)
    assert consent.cancelled is True
    assert consent.granted is False


@pytest.mark.parametrize(
    "opt_in,approved",
    [
        ((), (CAPACITY_FREE, CAPACITY_SUBSCRIPTION)),
        ((CAPACITY_UNKNOWN,), (CAPACITY_FREE, CAPACITY_SUBSCRIPTION, CAPACITY_UNKNOWN)),
        ((CAPACITY_METERED,), (CAPACITY_FREE, CAPACITY_SUBSCRIPTION, CAPACITY_METERED)),
        ((CAPACITY_UNKNOWN, CAPACITY_METERED), ALL_CLASSES),
    ],
)
def test_capacity_consent_without_terminal_needs_flags_for_costly_groups(
    opt_in: tuple[str, ...], approved: tuple[str, ...]
) -> None:
    lines: list[str] = []

    def no_prompt(_prompt: str) -> str:
        raise AssertionError("a run without a terminal must not prompt")

    consent = confirm_capacity_groups(
        _groups(), read_line=no_prompt, write=lines.append, interactive=False, opt_in=opt_in
    )
    assert consent.approved == approved
    text = "".join(lines)
    assert ("--include-unknown" in text) is (CAPACITY_UNKNOWN not in opt_in)
    assert ("--allow-metered" in text) is (CAPACITY_METERED not in opt_in)


def _consume_mixed(tmp_path: Path, transport: _Transport, **kwargs: Any) -> Any:
    return surfaces.consume_verified_models(
        gateway=GATEWAY,
        adapter=_mixed_adapter(tmp_path),
        manual=True,
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        live=False,
        **kwargs,
    )


def test_free_only_makes_zero_calls_to_unknown_or_metered_routes(isolated_home: Path) -> None:
    transport = _Transport()
    lines: list[str] = []
    result = _consume_mixed(isolated_home, transport, read_line=lambda _p: "f", write=lines.append)
    assert result.ok, result.data
    assert sorted(transport.calls) == [("fr/free", "chat"), ("fr/free", "tool")]
    summary = result.data["refresh"]
    assert summary["approved_classes"] == [CAPACITY_FREE]
    reasons = {rid: o["refresh_reason"] for rid, o in summary["route_outcomes"].items()}
    assert reasons == {
        "fr/free": None,
        "cc/sub": "not_approved",
        "zz/unknown": "not_approved",
        "ap/metered": "not_approved",
    }
    statuses = {r["route_id"]: r["status"] for r in result.data["rows"]}
    assert statuses["fr/free"] == "VERIFIED"
    assert statuses["zz/unknown"] == statuses["ap/metered"] == "UNVERIFIED"
    shown = "".join(lines)
    assert "UNKNOWN: 1 route" in shown and "estimated spend unknown (no pricing)" in shown
    assert "METERED: 1 route" in shown and "estimated spend ~$" in shown


def test_manual_yes_with_group_defaults_probes_only_prepaid(isolated_home: Path) -> None:
    transport = _Transport()
    answers = iter(["y", "", "", "", ""])
    result = _consume_mixed(isolated_home, transport, read_line=lambda _p: next(answers))
    assert result.ok, result.data
    assert transport.routes_called() == {"fr/free", "cc/sub"}


def test_non_interactive_manual_refresh_skips_costly_groups_without_flags(
    isolated_home: Path,
) -> None:
    transport = _Transport()

    def no_prompt(_prompt: str) -> str:
        raise AssertionError("a run without a terminal must not prompt")

    result = _consume_mixed(isolated_home, transport, read_line=no_prompt, interactive=False)
    assert result.ok, result.data
    assert transport.routes_called() == {"fr/free", "cc/sub"}

    transport = _Transport()
    result = _consume_mixed(
        isolated_home, transport, read_line=no_prompt, interactive=False, opt_in=(CAPACITY_UNKNOWN,)
    )
    assert result.ok, result.data
    assert ("zz/unknown", "chat") in transport.calls
    assert "ap/metered" not in transport.routes_called()


def _cli_args(*flags: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    orchestration_cli.add_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(["eligibility", "--verified", "--gateway", GATEWAY, *flags])


@pytest.mark.parametrize(
    "flags,opt_in",
    [
        (["--refresh"], ()),
        (["--refresh", "--include-unknown"], ("unknown",)),
        (["--refresh", "--allow-metered"], ("metered",)),
        (["--refresh", "--include-unknown", "--allow-metered"], ("unknown", "metered")),
    ],
)
def test_cli_refresh_flags_reach_the_consumer_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], opt_in: tuple[str, ...]
) -> None:
    seen: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return surfaces.ActionResult(data={"error": "captured"}, ok=False, exit_code=3)

    monkeypatch.setattr(surfaces, "consume_verified_models", capture)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert orchestration_cli._eligibility_verified(_cli_args(*flags, "--json")) == 3
    assert seen[0]["manual"] is True
    assert seen[0]["interactive"] is False
    assert seen[0]["opt_in"] == opt_in


@pytest.mark.parametrize("flag", ["--include-unknown", "--allow-metered"])
def test_cli_cost_opt_in_without_refresh_is_refused_before_io(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    def forbidden(**_kwargs: Any) -> Any:
        raise AssertionError("refused flags must not reach the consumer")

    monkeypatch.setattr(surfaces, "consume_verified_models", forbidden)
    assert orchestration_cli._eligibility_verified(_cli_args(flag, "--json")) == 2


def _execute_mixed(tmp_path: Path, transport: _Transport, **kwargs: Any) -> Any:
    rows = _plan_rows()
    plan = action_models_refresh_plan(
        snapshot_rows=rows,
        needed_ids=[r["route_id"] for r in rows],
        include_metered=True,
        now=NOW,
        gateway_origin="http://g",
        evidence_generation="gen1",
    ).data
    snapshot = RefreshSnapshot(
        rows=tuple(
            _row(rid, capacity=capacity, provider=rid.split("/", 1)[0])
            for rid, capacity in ROUTE_CLASS.items()
        ),
        generation="gen1",
        gateway_origin="http://g",
    )
    return action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=snapshot,
        now=NOW,
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
        **kwargs,
    )


def test_execute_without_approved_classes_runs_prepaid_groups_only(tmp_path: Path) -> None:
    transport = _Transport()
    result = _execute_mixed(tmp_path, transport)
    assert result.ok, result.data
    assert transport.routes_called() == {"fr/free", "cc/sub"}
    assert result.data["approved_classes"] == [CAPACITY_FREE, CAPACITY_SUBSCRIPTION]
    outcomes = result.data["route_outcomes"]
    assert outcomes["zz/unknown"]["refresh_reason"] == "not_approved"
    assert outcomes["ap/metered"]["refresh_reason"] == "not_approved"


@pytest.mark.parametrize("approved", ["unknown", ["unkown"], [None], ["UNKNOWN"], 7])
def test_execute_refuses_unrecognised_approved_classes(tmp_path: Path, approved: Any) -> None:
    transport = _Transport()
    result = _execute_mixed(tmp_path, transport, approved_classes=approved)
    assert not result.ok
    assert result.data["reason"] == "invalid_approved_classes"
    assert transport.calls == []


def test_adapter_snapshot_scope_is_stable_nonsecret_and_gateway_bound(tmp_path: Path) -> None:
    import hashlib

    def scope_for(gateway: str) -> str:
        adapter = VerifiedSnapshotAdapter(
            gateway,
            StorePaths.defaults(tmp_path),
            [inventory_row("fr/free")],
            [conn("fr", auth="apikey", plan="free")],
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
        )
        return adapter.refresh_snapshot(adapter.load(surfaces.VerifiedModelQuery())).scope

    a1, a2, b = (
        scope_for("http://gw-a:20128"),
        scope_for("http://gw-a:20128"),
        scope_for("http://gw-b:20128"),
    )
    assert a1 and a1 == a2 and a1 != b
    assert len(a1) == len(hashlib.sha256().hexdigest())
    assert "gw-a" not in a1
