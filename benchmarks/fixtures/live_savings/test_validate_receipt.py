"""Test validate_cooldown_receipt against ADR-0447 rules."""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sol = importlib.import_module("solution")

def test_valid_receipt():
    r = {
        "sentinel": "COOLDOWN_SENTINEL_7QX",
        "reset_minutes": 4380,
        "cooldown_clamp_reason": "provider_string_untrusted",
    }
    assert sol.validate_cooldown_receipt(r) is True

def test_wrong_sentinel():
    r = {
        "sentinel": "WRONG",
        "reset_minutes": 4380,
        "cooldown_clamp_reason": "provider_string_untrusted",
    }
    assert sol.validate_cooldown_receipt(r) is False

def test_unclamped_over_max():
    r = {
        "sentinel": "COOLDOWN_SENTINEL_7QX",
        "reset_minutes": 9999,
        "cooldown_clamp_reason": "provider_string_untrusted",
    }
    assert sol.validate_cooldown_receipt(r) is False

def test_missing_clamp_reason():
    r = {
        "sentinel": "COOLDOWN_SENTINEL_7QX",
        "reset_minutes": 4380,
    }
    assert sol.validate_cooldown_receipt(r) is False
