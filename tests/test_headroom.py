"""Tests for provider headroom checks."""

from unittest.mock import MagicMock

from verdict.headroom import check_headroom
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
