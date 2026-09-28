"""Tests for GlobalConcurrencyGovernor (BOD-157 / BOD-203).

Covers: admit-up-to-cap, exceed-cap-deferred, resource-pressure-reduces,
provider-pool-exhausted-defers, in-flight starvation protection, fail-closed
on unknown evidence, caps-never-below-1.
"""

from __future__ import annotations

import pytest

from verdict.orchestration.concurrency_governor import (
    AdmitRequest,
    Decision,
    GlobalConcurrencyGovernor,
    GovernorCaps,
    GovernorResult,
    ResourcePressure,
    RunState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gov(
    max_stories: int = 3, max_coding_workers: int = 6, max_integration_slots: int = 2
) -> GlobalConcurrencyGovernor:
    return GlobalConcurrencyGovernor(
        GovernorCaps(
            max_stories=max_stories,
            max_coding_workers=max_coding_workers,
            max_integration_slots=max_integration_slots,
        )
    )


def _req(
    story_id: str = "story-1", coding: bool = False, integration: bool = False, pool: str = ""
) -> AdmitRequest:
    return AdmitRequest(
        story_id=story_id,
        needs_coding_worker=coding,
        needs_integration_slot=integration,
        provider_pool=pool,
    )


# ---------------------------------------------------------------------------
# AC: admit up to cap
# ---------------------------------------------------------------------------


class TestAdmitUpToCap:
    def test_first_story_admitted(self) -> None:
        gov = _gov(max_stories=3)
        result = gov.admit(_req(), RunState())
        assert result.decision is Decision.ADMIT

    def test_second_story_admitted(self) -> None:
        gov = _gov(max_stories=3)
        state = RunState(running_stories=1)
        result = gov.admit(_req(story_id="story-2"), state)
        assert result.decision is Decision.ADMIT

    def test_at_boundary_still_admitted(self) -> None:
        gov = _gov(max_stories=3)
        state = RunState(running_stories=2)
        result = gov.admit(_req(story_id="story-3"), state)
        assert result.decision is Decision.ADMIT

    def test_coding_worker_admitted_under_cap(self) -> None:
        gov = _gov(max_coding_workers=4)
        state = RunState(running_coding_workers=3)
        result = gov.admit(_req(coding=True), state)
        assert result.decision is Decision.ADMIT

    def test_integration_slot_admitted_under_cap(self) -> None:
        gov = _gov(max_integration_slots=2)
        state = RunState(running_integration_slots=1)
        result = gov.admit(_req(integration=True), state)
        assert result.decision is Decision.ADMIT


# ---------------------------------------------------------------------------
# AC: over cap deferred / serialized
# ---------------------------------------------------------------------------


class TestOverCap:
    def test_story_cap_exceeded_serializes(self) -> None:
        gov = _gov(max_stories=2)
        state = RunState(running_stories=2)
        result = gov.admit(_req(story_id="story-3"), state)
        assert result.decision is Decision.SERIALIZE

    def test_coding_worker_cap_exceeded_defers(self) -> None:
        gov = _gov(max_coding_workers=2)
        state = RunState(running_coding_workers=2)
        result = gov.admit(_req(coding=True), state)
        assert result.decision is Decision.DEFER

    def test_integration_slot_cap_exceeded_defers(self) -> None:
        gov = _gov(max_integration_slots=1)
        state = RunState(running_integration_slots=1)
        result = gov.admit(_req(integration=True), state)
        assert result.decision is Decision.DEFER

    def test_non_coding_request_ignores_coding_cap(self) -> None:
        """A request that doesn't need a coding slot passes even if coding is full."""
        gov = _gov(max_coding_workers=1)
        state = RunState(running_coding_workers=99)
        result = gov.admit(_req(coding=False), state)
        assert result.decision is Decision.ADMIT


# ---------------------------------------------------------------------------
# AC: resource pressure reduces effective cap
# ---------------------------------------------------------------------------


class TestResourcePressure:
    def test_moderate_pressure_reduces_story_cap(self) -> None:
        gov = _gov(max_stories=4)
        # 50% pressure → effective cap = int(4 * 0.5) = 2
        pressure = ResourcePressure(cpu=0.5)
        state = RunState(running_stories=2)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.SERIALIZE

    def test_moderate_pressure_still_admits_below_effective(self) -> None:
        gov = _gov(max_stories=4)
        pressure = ResourcePressure(cpu=0.5)
        state = RunState(running_stories=1)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.ADMIT

    def test_high_ram_pressure_reduces_coding_cap(self) -> None:
        gov = _gov(max_coding_workers=6)
        # 80% RAM → effective = int(6 * 0.2) = 1
        pressure = ResourcePressure(ram=0.8)
        state = RunState(running_coding_workers=1)
        result = gov.admit(_req(coding=True), state, pressure)
        assert result.decision is Decision.DEFER

    def test_disk_pressure_considered(self) -> None:
        gov = _gov(max_stories=4)
        pressure = ResourcePressure(disk=0.9)
        # effective = int(4 * 0.1) = max(1, 0) = 1
        state = RunState(running_stories=1)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.SERIALIZE

    def test_max_pressure_across_dimensions(self) -> None:
        """The highest pressure dimension governs."""
        gov = _gov(max_stories=4)
        pressure = ResourcePressure(cpu=0.1, ram=0.9, disk=0.0)
        # max pressure is 0.9 → effective = max(1, int(4 * 0.1)) = 1
        state = RunState(running_stories=1)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.SERIALIZE

    def test_full_pressure_floor_is_one(self) -> None:
        """Even at pressure 1.0, effective cap is 1, not 0."""
        gov = _gov(max_stories=5)
        pressure = ResourcePressure(cpu=1.0)
        state = RunState(running_stories=0)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.ADMIT

    def test_nan_pressure_treated_as_max(self) -> None:
        """NaN pressure fails closed (treated as 1.0)."""
        gov = _gov(max_stories=4)
        pressure = ResourcePressure(cpu=float("nan"))
        # effective = max(1, int(4 * 0.0)) = 1
        state = RunState(running_stories=1)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.SERIALIZE


# ---------------------------------------------------------------------------
# AC: exhausted provider pool defers
# ---------------------------------------------------------------------------


class TestProviderPool:
    def test_healthy_pool_admitted(self) -> None:
        gov = _gov()
        state = RunState(pool_healthy_capacity={"anthropic": 5})
        result = gov.admit(_req(pool="anthropic"), state)
        assert result.decision is Decision.ADMIT

    def test_exhausted_pool_defers(self) -> None:
        gov = _gov()
        state = RunState(pool_healthy_capacity={"anthropic": 0})
        result = gov.admit(_req(pool="anthropic"), state)
        assert result.decision is Decision.DEFER
        assert "0 healthy" in result.reason

    def test_unknown_pool_defers_fail_closed(self) -> None:
        gov = _gov()
        state = RunState(pool_healthy_capacity={})
        result = gov.admit(_req(pool="unknown-provider"), state)
        assert result.decision is Decision.DEFER
        assert "unknown" in result.reason

    def test_no_pool_requirement_ignores_pool_health(self) -> None:
        gov = _gov()
        state = RunState(pool_healthy_capacity={"anthropic": 0})
        result = gov.admit(_req(pool=""), state)
        assert result.decision is Decision.ADMIT

    def test_pool_check_precedes_cap_check(self) -> None:
        """Pool exhaustion takes priority over cap checks."""
        gov = _gov(max_stories=10)
        state = RunState(running_stories=0, pool_healthy_capacity={"anthropic": 0})
        result = gov.admit(_req(pool="anthropic"), state)
        assert result.decision is Decision.DEFER


# ---------------------------------------------------------------------------
# AC: in-flight starvation protection — cap never below 1 for running story
# ---------------------------------------------------------------------------


class TestStarvationProtection:
    def test_already_running_story_admitted_over_cap(self) -> None:
        gov = _gov(max_stories=1)
        state = RunState(running_stories=5, story_is_already_running=True)
        result = gov.admit(_req(), state)
        assert result.decision is Decision.ADMIT
        assert "starvation" in result.reason

    def test_already_running_admitted_despite_pressure(self) -> None:
        gov = _gov(max_stories=2)
        state = RunState(running_stories=5, story_is_already_running=True)
        pressure = ResourcePressure(cpu=1.0, ram=1.0, disk=1.0)
        result = gov.admit(_req(), state, pressure)
        assert result.decision is Decision.ADMIT

    def test_already_running_admitted_despite_coding_cap(self) -> None:
        gov = _gov(max_coding_workers=1)
        state = RunState(running_coding_workers=10, story_is_already_running=True)
        result = gov.admit(_req(coding=True), state)
        assert result.decision is Decision.ADMIT


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_default_caps(self) -> None:
        gov = GlobalConcurrencyGovernor()
        assert gov.caps.max_stories == 3
        assert gov.caps.max_coding_workers == 6
        assert gov.caps.max_integration_slots == 2

    def test_caps_clamped_to_one(self) -> None:
        caps = GovernorCaps(max_stories=0, max_coding_workers=-5, max_integration_slots=0)
        assert caps.max_stories == 1
        assert caps.max_coding_workers == 1
        assert caps.max_integration_slots == 1

    def test_pressure_clamped(self) -> None:
        p = ResourcePressure(cpu=-0.5, ram=2.0, disk=0.5)
        assert p.max_pressure() == 1.0  # ram 2.0 clamped to 1.0

    def test_result_is_frozen(self) -> None:
        r = GovernorResult(Decision.ADMIT, "ok")
        with pytest.raises(AttributeError):
            r.decision = Decision.DEFER  # type: ignore[misc]

    def test_no_pressure_means_full_caps(self) -> None:
        gov = _gov(max_stories=3)
        state = RunState(running_stories=2)
        result = gov.admit(_req(), state)
        assert result.decision is Decision.ADMIT

    def test_pool_defers_even_for_already_running(self) -> None:
        """Pool exhaustion beats starvation protection — can't run on a dead pool."""
        gov = _gov()
        state = RunState(story_is_already_running=True, pool_healthy_capacity={"dead-pool": 0})
        result = gov.admit(_req(pool="dead-pool"), state)
        assert result.decision is Decision.DEFER
