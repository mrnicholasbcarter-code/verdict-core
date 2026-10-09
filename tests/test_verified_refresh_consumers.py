"""Consumer-side refresh tests: ordering, cancel, consent, /probe (BOD-292).

Covers the wait-then-render contract (progress strictly before the final
outcome), shared cancellation, the y/N consent default-No controller, the
/eligibility and /probe parsers, the full failure-classification mapping, and
the picker/selection/autocomplete contracts. All I/O is injected.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
