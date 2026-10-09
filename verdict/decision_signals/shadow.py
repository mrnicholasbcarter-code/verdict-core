"""SHADOW mode integration helpers (TYPESAFE credentials migration)."""

from __future__ import annotations

import logging
import os
import warnings

logger = logging.getLogger(__name__)

# Modes that enable signal collection.
_COLLECT_MODES = frozenset({"SHADOW", "ADVISORY"})


def get_signals_mode() -> str:
    """Return the normalised VERDICT_DECISION_SIGNALS_MODE value.

    Returns:
        "OFF", "SHADOW", or "ADVISORY".  Invalid values -> "OFF" with a warning.
    """
    raw = os.environ.get("VERDICT_DECISION_SIGNALS_MODE", "OFF").strip().upper()
    if raw not in ("OFF", "SHADOW", "ADVISORY"):
        warnings.warn(
            f"Invalid VERDICT_DECISION_SIGNALS_MODE={raw!r}, treating as OFF. "
            f"Allowed: OFF, SHADOW, ADVISORY",
            UserWarning,
            stacklevel=2,
        )
        return "OFF"
    return raw


def should_collect_signals() -> bool:
    """Return True when the mode is SHADOW or ADVISORY (signals should be collected).

    SHADOW: one call per orchestrate run, never changes routing.
    ADVISORY: one call per route call (for non-protected, non-restricted tasks).
    Both modes record signals in EventLog; neither reorders routing outcomes.
    """
    return get_signals_mode() in _COLLECT_MODES


__all__ = ["get_signals_mode", "should_collect_signals"]
