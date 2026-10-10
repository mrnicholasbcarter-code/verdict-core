"""Bounded capability families: cheap variant tokens must not match Gemini."""

import pytest

from verdict.classifier import classify, classify_known


@pytest.mark.parametrize(
    "route",
    [
        "gemini-2.5-pro",
        "antigravity/gemini-3-pro",
        "agy/gemini-3.1-pro-high",
        "antigravity/gemini-3.1-pro-low",
        "google/gemini-2.5-pro-preview",
    ],
)
def test_known_gemini_pro_is_sufficient_for_reasoning_work(route):
    assert classify_known(route) == 1


@pytest.mark.parametrize(
    "route",
    [
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-nano",
        "o3-mini",
        "gpt-5.5-mini",
        "gpt-6.1-sol-mini",
        "mini-chat",
        "family_mini_high",
    ],
)
def test_cheap_variants_stay_cheap(route):
    assert classify_known(route) == 3


def test_existing_explicit_medium_mini_variant_is_preserved():
    assert classify_known("gpt-4o-mini") == 2
    assert classify_known("minimax-m2") is None


@pytest.mark.parametrize("route", ["cx/gpt-6-sol", "cx/gpt-6.1-sol", "cx/gpt-6.1-sol-high"])
def test_known_sol_family_is_frontier(route):
    assert classify_known(route) == 0


@pytest.mark.parametrize(
    "route",
    [
        "gpt-6.2-sol",
        "gemini-4-pro",
        "gemini-3.1-project",
        "gpt-6.1-solar",
        "gemini-3.1-prototype",
        "gpt-6.1-sol-unrecognized",
        "gemini-3.1-pro-fiction",
    ],
)
def test_unknown_families_do_not_gain_capability(route):
    assert classify_known(route) is None
    assert classify(route) == 2  # legacy convenience default is not admission proof


def test_actual_eligibility_accepts_pro_but_rejects_flash_for_tier_two(tmp_path):
    from tests.test_orch_eligibility import NOW, conn, make_ladder, row
    from verdict.orchestration.contracts import EligibilityStage, TaskRequirements

    pro, flash = "antigravity/gemini-3.1-pro-high", "antigravity/gemini-3.1-flash-lite"
    ladder, _probe = make_ladder(tmp_path, [row(pro), row(flash)], [conn("antigravity")])
    req = TaskRequirements(
        required_capabilities=frozenset({"tools", "reasoning"}),
        max_capability_tier=2,
        coding=True,
        reasoning=True,
    )
    verdicts = {v.route_id: v for v in ladder.evaluate(req, now=NOW)}
    assert verdicts[pro].failed_stage is None
    assert verdicts[flash].failed_stage is EligibilityStage.TASK_ELIGIBLE
    assert verdicts[flash].reason == "insufficient_capability"
