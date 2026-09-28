"""Test that drop_codes returns the four named drop codes from ADR-0448."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sol = importlib.import_module("solution")


def test_returns_four_codes():
    codes = sol.drop_codes()
    assert isinstance(codes, list), f"expected list, got {type(codes)}"
    assert len(codes) == 4, f"expected 4 codes, got {len(codes)}"


def test_correct_codes():
    codes = sol.drop_codes()
    expected = [
        "DROP_CAP_UNKNOWN",
        "DROP_PASSPORT_STALE_9F",
        "DROP_CONFIRM_BUDGET_2M",
        "DROP_INVENTORY_GHOST",
    ]
    assert codes == expected, f"expected {expected}, got {codes}"
