"""Failing specs for the controller primary/backup switcher (spec v6.1 section 4).

Hysteresis numbers follow the D6 proposal and are parameters, not constants.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from verdict.omniroute_pipeline.switcher import (
    Candidate,
    SwitchPolicy,
    SwitchState,
    build_pool,
    evaluate_switch,
    recommend_combo_order,
)

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _c(route: str, provider: str, elo: int | None, ctx: int, **kw: object) -> Candidate:
    base: dict[str, object] = {"eligible": True, "tool_ok": True, "free": True, "tos_avoid": False}
    base.update(kw)
    return Candidate(route=route, provider=provider, elo=elo, usable_context=ctx, **base)  # type: ignore[arg-type]


POOL = [
    _c("nvidia/z-ai/glm-5.3", "nvidia", 1479, 825_000),
    _c("nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia", 1416, 825_000),
    _c("kc/nvidia/nemotron-3.5-super-vl:free", "kc", 1416, 700_000),
    _c("agy/gemini-3.8-flash-high", "agy", 1496, 1_000_000, tos_avoid=True),
    _c("agentrouter/deepseek-v4-flash", "agentrouter", None, 700_000, free=False),
]


def test_pool_requires_825k_and_prefers_cross_provider_backup() -> None:
    pool = build_pool(POOL, min_context=825_000)
    assert [c.route for c in pool.members] == [
        "nvidia/z-ai/glm-5.3",
        "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
    ]
    relaxed = build_pool(POOL, min_context=700_000)
    assert relaxed.primary.route == "nvidia/z-ai/glm-5.3"
    assert relaxed.backup is not None and relaxed.backup.provider == "kc"


def test_two_soft_or_one_hard_switch() -> None:
    pol = SwitchPolicy()
    st = SwitchState(primary="nvidia/z-ai/glm-5.3", backup="kc/nvidia/nemotron-3.5-super-vl:free")
    st = evaluate_switch(st, pol, event="soft", now=T0)
    assert st.primary == "nvidia/z-ai/glm-5.3"
    st = evaluate_switch(st, pol, event="soft", now=T0 + timedelta(minutes=10))
    assert st.primary == "kc/nvidia/nemotron-3.5-super-vl:free"
    st2 = SwitchState(primary="a/x", backup="b/y")
    st2 = evaluate_switch(st2, pol, event="hard", now=T0)
    assert st2.primary == "b/y"


def test_soft_failures_outside_window_do_not_switch() -> None:
    pol = SwitchPolicy()
    st = SwitchState(primary="a/x", backup="b/y")
    st = evaluate_switch(st, pol, event="soft", now=T0)
    st = evaluate_switch(st, pol, event="soft", now=T0 + timedelta(minutes=16))
    assert st.primary == "a/x"


def test_promote_3_passes_30min_15_elo() -> None:
    pol = SwitchPolicy()
    st = SwitchState(primary="a/x", backup="b/y", primary_elo=1416)
    for i in range(3):
        st = evaluate_switch(
            st,
            pol,
            event="pass",
            now=T0 + timedelta(minutes=15 * i),
            challenger="c/z",
            challenger_elo=1430,
        )
    assert st.primary == "a/x"  # lead only 14
    st = SwitchState(primary="a/x", backup="b/y", primary_elo=1416)
    for i in range(3):
        st = evaluate_switch(
            st,
            pol,
            event="pass",
            now=T0 + timedelta(minutes=10 * i),
            challenger="c/z",
            challenger_elo=1479,
        )
    assert st.primary == "a/x"  # only 20 min span
    st = evaluate_switch(
        st, pol, event="pass", now=T0 + timedelta(minutes=31), challenger="c/z", challenger_elo=1479
    )
    assert st.primary == "c/z"


def test_flap_freeze() -> None:
    pol = SwitchPolicy()
    st = SwitchState(primary="a/x", backup="b/y", spare=("c/z", "d/w"))
    st = evaluate_switch(st, pol, event="hard", now=T0)
    st = evaluate_switch(st, pol, event="hard", now=T0 + timedelta(minutes=20))
    st = evaluate_switch(st, pol, event="hard", now=T0 + timedelta(minutes=40))
    assert st.frozen is True
    assert st.alert == "flap_freeze"


def test_empty_pool_fails_closed_no_agy_agentrouter() -> None:
    pool = build_pool([c for c in POOL if c.provider in {"agy", "agentrouter"}], min_context=0)
    assert pool.members == ()
    assert pool.primary is None
    assert pool.reason == "no controller route"


def test_combo_reorder_is_recommendation_only_without_ruling() -> None:
    rec = recommend_combo_order(
        "prime-free-chain", build_pool(POOL, min_context=700_000), ruled_write=False
    )
    assert rec.write_allowed is False
    assert rec.http_method is None
    assert "agy/gemini-3.8-flash-high" not in rec.order
