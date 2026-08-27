"""Tests for provider headroom checks."""

from unittest.mock import MagicMock

from verdict.headroom import UNKNOWN_HEADROOM, check_headroom, headroom_is_unknown
from verdict.models import ProviderConfig


def test_check_headroom_absent_endpoint():
    """When no headroom endpoint is configured, returns unknown (None)."""
    config = MagicMock(spec=ProviderConfig)
    config.headroom_endpoint = None

    result = check_headroom("gpt-4", "openai", config)

    assert result is None, "Absent endpoint should return unknown (None), not fabricated capacity"


def test_check_headroom_unimplemented_endpoint():
    """When headroom endpoint is configured but unimplemented, returns unknown (None)."""
    config = MagicMock(spec=ProviderConfig)
    config.headroom_endpoint = "https://api.example.com/usage"

    result = check_headroom("model-id", "provider-name", config)

    assert result is None, (
        "Unimplemented endpoint should return unknown (None), not fabricated capacity"
    )


def test_headroom_is_unknown_none():
    """headroom_is_unknown returns True only for None."""
    assert headroom_is_unknown(None) is True


def test_headroom_is_unknown_available():
    """headroom_is_unknown returns False for available capacity tuple."""
    assert headroom_is_unknown((True, 0.0)) is False


def test_headroom_is_unknown_unavailable():
    """headroom_is_unknown returns False for unavailable capacity tuple."""
    assert headroom_is_unknown((False, 100.0)) is False


def test_unknown_headroom_sentinel():
    """UNKNOWN_HEADROOM is the unknown sentinel treated as unknown by headroom_is_unknown."""
    assert UNKNOWN_HEADROOM is None, "UNKNOWN_HEADROOM must be None, not a fabricated tuple"
    assert headroom_is_unknown(UNKNOWN_HEADROOM) is True


def test_affordable_excludes_observed_capacity_below_estimated_unit_cost():
    """FR-029: some capacity available is not evidence the unit can finish."""
    from verdict.headroom import affordability

    verdict_ = affordability(estimated_tokens=8000, remaining_tokens=500)
    assert verdict_.admitted is False
    assert verdict_.state == "insufficient"
    assert "500" in verdict_.reason and "8000" in verdict_.reason


def test_affordable_admits_observed_capacity_at_or_above_estimate():
    from verdict.headroom import affordability

    exact = affordability(estimated_tokens=8000, remaining_tokens=8000)
    assert exact.admitted is True
    assert exact.state == "sufficient"


def test_affordability_unobserved_capacity_is_unknown_and_does_not_exclude():
    """FR-029/SC-008: unobserved capacity stays UNKNOWN and never excludes."""
    from verdict.headroom import affordability

    unknown = affordability(estimated_tokens=8000, remaining_tokens=None)
    assert unknown.admitted is True
    assert unknown.state == "UNKNOWN"


def test_affordability_without_estimate_cannot_exclude():
    """No estimated cost means no affordability evidence, so it must not exclude."""
    from verdict.headroom import affordability

    result = affordability(estimated_tokens=None, remaining_tokens=10)
    assert result.admitted is True
    assert result.state == "UNKNOWN"
