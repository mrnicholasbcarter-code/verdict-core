"""Controller primary/backup switcher (spec v6.1 section 4, phase e).

D6 (decided 08:49Z): drop to backup on 2 soft failures within 15 min or 1 hard;
promote after 3 consecutive passes spanning >= 30 min with >= +15 Elo; at most
2 switches per 2h, a 3rd trigger freezes and alerts; no re-promote for 60 min.
D12/D13 defaults: recommend-only on prime-free-chain; 700k gate, 650k compaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.xfail(
    strict=True, raises=ImportError, reason="phase e: switcher not implemented yet"
)

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _sw() -> ModuleType:
    import verdict.omniroute_pipeline.switcher as sw  # type: ignore[import-not-found]

    return sw


def _c(route: str, provider: str, elo: int | None, ctx: int, **kw: object) -> Any:
    base: dict[str, object] = {"eligible": True, "tool_ok": True, "free": True, "tos_avoid": False}
    base.update(kw)
    return _sw().Candidate(route=route, provider=provider, elo=elo, usable_context=ctx, **base)


def _pool() -> list[Any]:
    return [
        _c("nvidia/z-ai/glm-5.3", "nvidia", 1479, 825_000),
        _c("nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia", 1416, 825_000),
        _c("kc/nvidia/nemotron-3.5-super-vl:free", "kc", 1416, 700_000),
        # O1: agy is sanctioned, not ToS-avoid; usable_context unmeasured (0) until phase b.
        _c("agy/gemini-3.8-flash-high", "agy", 1496, 0),
        _c("kiro/x", "kiro", 1400, 1_000_000, tos_avoid=True),
        _c("agentrouter/deepseek-v4-flash", "agentrouter", None, 700_000, free=False),
    ]


def test_pool_requires_825k_and_prefers_cross_provider_backup() -> None:
    sw = _sw()
    pool = sw.build_pool(_pool(), min_context=825_000)
    assert [c.route for c in pool.members] == [
        "nvidia/z-ai/glm-5.3",
        "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
    ]
    relaxed = sw.build_pool(_pool(), min_context=700_000)
    assert relaxed.primary.route == "nvidia/z-ai/glm-5.3"
    assert relaxed.backup is not None and relaxed.backup.provider == "kc"


def test_default_min_context_700k_compact_650k() -> None:
    sw = _sw()
    cfg = sw.ControllerConfig()
    assert cfg.min_context == 700_000
    assert cfg.compact_at == 650_000
    pool = sw.build_pool(_pool())
    assert pool.backup is not None and pool.backup.route == "kc/nvidia/nemotron-3.5-super-vl:free"
    assert pool.compact_at == 650_000
    strict = sw.build_pool(_pool(), config=sw.ControllerConfig(min_context=825_000))
    assert all(c.usable_context >= 825_000 for c in strict.members)
    assert strict.compact_at is None


def test_two_soft_or_one_hard_switch() -> None:
    sw = _sw()
    pol = sw.SwitchPolicy()
    st = sw.SwitchState(
        primary="nvidia/z-ai/glm-5.3", backup="kc/nvidia/nemotron-3.5-super-vl:free"
    )
    st = sw.evaluate_switch(st, pol, event="soft", now=T0)
    assert st.primary == "nvidia/z-ai/glm-5.3"
    st = sw.evaluate_switch(st, pol, event="soft", now=T0 + timedelta(minutes=10))
    assert st.primary == "kc/nvidia/nemotron-3.5-super-vl:free"
    st2 = sw.evaluate_switch(sw.SwitchState(primary="a/x", backup="b/y"), pol, event="hard", now=T0)
    assert st2.primary == "b/y"


def test_soft_failures_outside_window_do_not_switch() -> None:
    sw = _sw()
    pol = sw.SwitchPolicy()
    st = sw.SwitchState(primary="a/x", backup="b/y")
    st = sw.evaluate_switch(st, pol, event="soft", now=T0)
    st = sw.evaluate_switch(st, pol, event="soft", now=T0 + timedelta(minutes=16))
    assert st.primary == "a/x"


def test_promote_3_passes_30min_15_elo() -> None:
    sw = _sw()
    pol = sw.SwitchPolicy()

    def run(elo: int, gaps: list[int]) -> Any:
        st = sw.SwitchState(primary="a/x", backup="b/y", primary_elo=1416)
        for minutes in gaps:
            st = sw.evaluate_switch(
                st,
                pol,
                event="pass",
                now=T0 + timedelta(minutes=minutes),
                challenger="c/z",
                challenger_elo=elo,
            )
        return st

    assert run(1430, [0, 15, 30]).primary == "a/x"  # +14 Elo
    assert run(1479, [0, 10, 20]).primary == "a/x"  # 20 min span
    assert run(1479, [0, 15, 30]).primary == "c/z"  # +63, 30 min, 3 passes


def test_no_repromote_within_60min() -> None:
    sw = _sw()
    pol = sw.SwitchPolicy()
    st = sw.SwitchState(primary="a/x", backup="b/y", primary_elo=1479)
    st = sw.evaluate_switch(st, pol, event="hard", now=T0)
    assert st.primary == "b/y"
    for minutes in (5, 25, 45):
        st = sw.evaluate_switch(
            st,
            pol,
            event="pass",
            now=T0 + timedelta(minutes=minutes),
            challenger="a/x",
            challenger_elo=1479,
        )
    assert st.primary == "b/y"


def test_flap_freeze() -> None:
    sw = _sw()
    pol = sw.SwitchPolicy()
    st = sw.SwitchState(primary="a/x", backup="b/y", spare=("c/z", "d/w"))
    for minutes in (0, 20, 40):
        st = sw.evaluate_switch(st, pol, event="hard", now=T0 + timedelta(minutes=minutes))
    assert st.frozen is True
    assert st.alert == "flap_freeze"
    assert len(st.switch_times) == 2


def test_empty_pool_fails_closed_no_kiro_agentrouter() -> None:
    sw = _sw()
    pool = sw.build_pool(
        [c for c in _pool() if c.provider in {"kiro", "agentrouter"}], min_context=0
    )
    assert pool.members == ()
    assert pool.primary is None
    assert pool.reason == "no controller route"


def test_combo_reorder_is_recommendation_only_without_ruling() -> None:
    sw = _sw()
    rec = sw.recommend_combo_order(
        "prime-free-chain", sw.build_pool(_pool(), min_context=700_000), ruled_write=False
    )
    assert rec.write_allowed is False
    assert rec.http_method is None


def test_agy_stays_second_in_combo_recommendation() -> None:
    sw = _sw()
    current = [
        "nvidia/z-ai/glm-5.3",
        "agy/gemini-3.8-flash-high",
        "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
        "kc/nvidia/nemotron-3.5-super-vl:free",
        "kc/reflection/beam-free",
    ]
    rec = sw.recommend_combo_order(
        "prime-free-chain", sw.build_pool(_pool()), ruled_write=True, current_order=current
    )
    assert rec.http_method is None  # the switcher never writes the combo
    assert rec.order.index("agy/gemini-3.8-flash-high") == 1  # O1: stays second
    assert "agy" not in rec.policy_flags  # no agy policy flag (O1)
