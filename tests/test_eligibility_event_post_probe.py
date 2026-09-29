"""Tests for eligibility event post-probe fields (BOD-203).

Verifies that:
- select() return type is unchanged (2-tuple) vs origin/main
- last_select_stats exposes post-probe counts without changing any signature
"""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from verdict.orchestration.contracts import CapacityClass, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder, EligibilityStage


@pytest.fixture
def eligibility_ladder():
    """Create an EligibilityLadder with mock state."""
    ladder = EligibilityLadder.__new__(EligibilityLadder)
    ladder._ttl = 300.0
    ladder._max_probes = 8
    ladder._admitted = None
    ladder._state = {
        "inventory": {},
        "health": {},
        "confirmations": {},
        "cooldowns": {},
        "eligibility_policy": {},
    }
    ladder._last_verdicts = ()
    ladder._last_select_stats = {}
    ladder._probe = Mock(return_value=Mock(healthy=True, category="ok"))
    ladder._record_health = Mock()
    ladder._record_confirmation = Mock()
    ladder._active_cooldown = Mock(return_value=None)
    return ladder


def test_select_returns_two_tuple(eligibility_ladder):
    """select() must return exactly (selected, verdicts) — no breaking change."""
    requirements = TaskRequirements()
    now = datetime.now(timezone.utc)

    mock_assessment = Mock()
    mock_assessment.route_id = "kr/claude-opus"
    mock_assessment.provider = "kr"
    mock_assessment.health = "healthy"
    mock_assessment.failed_stage = None
    mock_assessment.capacity = CapacityClass.SUBSCRIPTION
    mock_assessment.plan_label = ""
    mock_assessment.verdict = Mock(
        return_value=Mock(
            route_id="kr/claude-opus", reached=EligibilityStage.TASK_ELIGIBLE, health="healthy"
        )
    )

    eligibility_ladder._assess_all = Mock(return_value=([mock_assessment], [mock_assessment]))
    eligibility_ladder._probe_order = Mock(return_value=[mock_assessment])

    result = eligibility_ladder.select(requirements, now=now)

    assert isinstance(result, tuple)
    assert len(result) == 2, f"select() must return 2-tuple, got {len(result)}-tuple"
    _selected, verdicts = result  # must unpack cleanly
    assert isinstance(verdicts, tuple)


def test_last_select_stats_populated_after_select(eligibility_ladder):
    """last_select_stats is populated after select() with expected keys."""
    requirements = TaskRequirements()
    now = datetime.now(timezone.utc)

    mock_assessment = Mock()
    mock_assessment.route_id = "kr/claude-opus"
    mock_assessment.provider = "kr"
    mock_assessment.health = "healthy"
    mock_assessment.failed_stage = None
    mock_assessment.capacity = CapacityClass.SUBSCRIPTION
    mock_assessment.plan_label = ""
    mock_assessment.verdict = Mock(
        return_value=Mock(
            route_id="kr/claude-opus", reached=EligibilityStage.TASK_ELIGIBLE, health="healthy"
        )
    )

    eligibility_ladder._assess_all = Mock(return_value=([mock_assessment], [mock_assessment]))
    eligibility_ladder._probe_order = Mock(return_value=[mock_assessment])

    assert eligibility_ladder.last_select_stats == {}

    eligibility_ladder.select(requirements, now=now)

    stats = eligibility_ladder.last_select_stats
    assert "probed" in stats
    assert "stale" in stats
    assert "healthy_after_probe" in stats
    assert "eligible_after_probe" in stats
    assert isinstance(stats["probed"], int)
    assert stats["probed"] >= 0


def test_last_select_stats_is_copy(eligibility_ladder):
    """last_select_stats returns a copy, not the internal dict."""
    requirements = TaskRequirements()
    now = datetime.now(timezone.utc)

    mock_assessment = Mock()
    mock_assessment.route_id = "kr/claude-opus"
    mock_assessment.provider = "kr"
    mock_assessment.health = "healthy"
    mock_assessment.failed_stage = None
    mock_assessment.capacity = CapacityClass.SUBSCRIPTION
    mock_assessment.plan_label = ""
    mock_assessment.verdict = Mock(
        return_value=Mock(
            route_id="kr/claude-opus", reached=EligibilityStage.TASK_ELIGIBLE, health="healthy"
        )
    )

    eligibility_ladder._assess_all = Mock(return_value=([mock_assessment], [mock_assessment]))
    eligibility_ladder._probe_order = Mock(return_value=[mock_assessment])

    eligibility_ladder.select(requirements, now=now)

    stats1 = eligibility_ladder.last_select_stats
    stats2 = eligibility_ladder.last_select_stats
    assert stats1 is not stats2
    assert stats1 == stats2


def test_parity_select_return_vs_main(eligibility_ladder):
    """Parity: select() returns the same 2-tuple type as origin/main."""
    requirements = TaskRequirements()
    now = datetime.now(timezone.utc)

    mock_assessment = Mock()
    mock_assessment.route_id = "kr/claude-opus"
    mock_assessment.provider = "kr"
    mock_assessment.health = "healthy"
    mock_assessment.failed_stage = None
    mock_assessment.capacity = CapacityClass.SUBSCRIPTION
    mock_assessment.plan_label = ""

    selected_verdict = Mock(
        route_id="kr/claude-opus", reached=EligibilityStage.SELECTED, health="healthy"
    )
    mock_assessment.verdict = Mock(return_value=selected_verdict)

    eligibility_ladder._assess_all = Mock(return_value=([mock_assessment], [mock_assessment]))
    eligibility_ladder._probe_order = Mock(return_value=[mock_assessment])

    result = eligibility_ladder.select(requirements, now=now)

    # Must be exactly 2-tuple (same as origin/main)
    assert len(result) == 2
    _sel, vdcts = result
    # verdicts is a tuple
    assert isinstance(vdcts, tuple)
