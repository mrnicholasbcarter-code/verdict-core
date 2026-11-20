"""Shared price parser for discovery source adapters.

All adapters that report pricing (OpenRouter, Kilo, models.dev, OmniRoute)
import :func:`parse_price` from here so the behaviour is consistent.

API price values arrive as int, float, or numeric string (e.g. ``"0"``
or ``"0.000001"``).  ``parse_price`` normalises them all to float, or
returns ``None`` for absent, non-numeric, negative, or non-finite values.
"""

from __future__ import annotations

import math
from typing import Any


def parse_price(value: Any) -> float | None:
    """Parse a model price value from API data.

    Accepts int, float, or numeric strings (e.g. ``"0"``, ``"0.000001"``).
    Returns ``None`` for ``None``, booleans, negative values, non-finite
    values (``inf``, ``-inf``, ``"infinity"``, ``"1e999"``), or non-numeric
    strings such as ``"abc"``.

    Args:
        value: Raw price value from an API response.

    Returns:
        Non-negative finite float, or ``None`` if the value is absent, invalid,
        or non-finite.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            return f if f >= 0 and math.isfinite(f) else None
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            f = float(value)
            return f if f >= 0 and math.isfinite(f) else None
        except ValueError:
            return None
    return None
