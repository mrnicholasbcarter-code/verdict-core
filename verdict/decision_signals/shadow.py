"""SHADOW mode integration helpers (BOD-199)."""

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


def should_collect_signals() -> bool:
    """Check if decision signal collection is enabled.

    Returns:
        True if mode is SHADOW, False otherwise (including OFF and invalid values)
    """
    mode = os.environ.get("VERDICT_DECISION_SIGNALS_MODE", "OFF").strip().upper()

    if mode not in ("OFF", "SHADOW"):
        warnings.warn(
            f"Invalid VERDICT_DECISION_SIGNALS_MODE={mode!r}, treating as OFF. "
            f"Allowed: OFF, SHADOW",
            UserWarning,
            stacklevel=2,
        )
        return False

    return mode == "SHADOW"


def collect_shadow_signals(
    question: DecisionQuestionV1,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, str], bytes]]
    | None = None,
) -> DecisionSignalSetV1:
    """Collect decision signals in SHADOW mode.

    Args:
        question: Decision question
        base_url: Optional OpenJev base URL (default: from env)
        api_key: Optional OpenJev API key (default: from env)
        transport: Optional injectable transport for testing

    Returns:
        DecisionSignalSetV1 (never raises; failures as signal set with failure_class)
    """
    now = datetime.now(timezone.utc)

    provider = OpenJevSystemOneProvider(base_url=base_url, api_key=api_key, transport=transport)
    return provider.signals(question, now=now)


__all__ = ["collect_shadow_signals", "should_collect_signals"]
