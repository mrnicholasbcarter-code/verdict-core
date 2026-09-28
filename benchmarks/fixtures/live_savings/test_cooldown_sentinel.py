"""Test cooldown_info returns sentinel and clamp from ADR-0447."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sol = importlib.import_module("solution")


def test_sentinel():
    info = sol.cooldown_info()
    assert isinstance(info, dict), f"expected dict, got {type(info)}"
    assert info["sentinel"] == "COOLDOWN_SENTINEL_7QX"


def test_clamp_minutes():
    info = sol.cooldown_info()
    assert info["clamp_minutes"] == 4380


def test_clamp_reason():
    info = sol.cooldown_info()
    assert info["clamp_reason"] == "provider_string_untrusted"
