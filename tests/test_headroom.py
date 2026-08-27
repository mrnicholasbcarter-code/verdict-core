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

    assert result is None, "Unimplemented endpoint should return unknown (None), not fabricated capacity"


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
    assert headroom_is_unknown(UNKNOWN_HEADROOM) is True
