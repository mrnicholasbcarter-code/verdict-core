"""Shared price parser for discovery source adapters.

All adapters that report pricing (OpenRouter, Kilo, models.dev, OmniRoute)
import :func:`parse_price` from here so the behaviour is consistent.

API price values arrive as int, float, or numeric string (e.g. ``"0"``
or ``"0.000001"``).  ``parse_price`` normalises them all to float, or
returns ``None`` for absent, non-numeric, or negative values.
"""

from __future__ import annotations

from typing import Any


def parse_price(value: Any) -> float | None:
    """Parse a model price value from API data.

    Accepts int, float, or numeric strings (e.g. ``"0"``, ``"0.000001"``).
    Returns ``None`` for ``None``, booleans, negative values, or
    non-numeric strings such as ``"abc"``.

    Args:
        value: Raw price value from an API response.

    Returns:
        Non-negative float, or ``None`` if the value is absent or invalid.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            return f if f >= 0 else None
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            f = float(value)
            return f if f >= 0 else None
        except ValueError:
            return None
    return None
