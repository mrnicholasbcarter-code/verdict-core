"""R5 declared-tier writes at free_tier_admit.py:916 (spec v6.1 phase g).

Gated on BOD-419 merging plus a controller go (D7/D18). classifier.py is not
touched here. Skipped, not xfailed, so nothing in this branch depends on it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="phase g: gated on BOD-419 + controller go (D7/D18)")


def test_declared_tier_written_tier0_pattern_only() -> None:
    raise AssertionError("write when BOD-419 lands")


def test_pattern_fallback_when_unmapped() -> None:
    raise AssertionError("write when BOD-419 lands")
