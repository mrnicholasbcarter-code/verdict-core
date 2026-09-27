"""SHADOW mode integration helpers (TYPESAFE credentials migration)."""

from __future__ import annotations

import logging
import os
import warnings
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from verdict.decision_signals.contracts import DecisionQuestionV1, DecisionSignalSetV1
from verdict.decision_signals.openjev import OpenJevSystemOneProvider

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


def collect_shadow_signals(
    question: DecisionQuestionV1,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, str], bytes]]
    | None = None,
) -> DecisionSignalSetV1:
    """Collect decision signals in SHADOW or ADVISORY mode.

    Args:
        question: Decision question
        base_url: Optional Codiv base URL (default: from TYPESAFE_BASE_URL env)
        api_key: Optional API key (default: from TYPESAFE_API_KEY env)
        transport: Optional injectable transport for testing

    Returns:
        DecisionSignalSetV1 (never raises; failures returned as signal set with failure_class)
    """
    now = datetime.now(timezone.utc)
    provider = OpenJevSystemOneProvider(base_url=base_url, api_key=api_key, transport=transport)
    return provider.signals(question, now=now)


__all__ = ["collect_shadow_signals", "get_signals_mode", "should_collect_signals"]
