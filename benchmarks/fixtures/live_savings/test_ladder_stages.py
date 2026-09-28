"""Test ladder_stages returns the six uplift ladder stages in order."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sol = importlib.import_module("solution")


def test_six_stages():
    stages = sol.ladder_stages()
    assert isinstance(stages, list)
    assert len(stages) == 6


def test_correct_order():
    stages = sol.ladder_stages()
    expected = ["SEEDED", "PROBED", "WARRANTED", "PACKED", "BOUND", "SERVED"]
    assert stages == expected, f"expected {expected}, got {stages}"


def test_warranted_metadata_rule():
    info = sol.ladder_constraints()
    assert info["metadata_stage"] == "WARRANTED"


def test_bound_identity_rule():
    info = sol.ladder_constraints()
    assert info["identity_stage"] == "BOUND"
