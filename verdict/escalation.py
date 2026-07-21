"""Keyword escalation scanner.

Scans task text for patterns that indicate higher-than-requested criticality.
Patterns are matched case-insensitively. The effective tier is the MINIMUM of
the requested tier and all matching pattern tiers (i.e., escalation only bumps UP).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from verdict.models import EscalationPattern

# fmt: off
DEFAULT_PATTERNS: list[EscalationPattern] = [
    # T0 — critical, never offload
    EscalationPattern(
        pattern=r"(payment|billing|charge|refund|stripe|invoice|subscription)",
        min_tier=0, label="money-path",
    ),
    EscalationPattern(
        pattern=r"(live.?order|place.?order|execute.?trade|real.?money|production.?deploy)",
        min_tier=0, label="live-execution",
    ),
    # T1 — high capability required
    EscalationPattern(
        pattern=r"(auth|login|token|session|password|jwt|oauth|credential|secret)",
        min_tier=1, label="auth-security",
    ),
    EscalationPattern(
        pattern=r"(migrat|schema|alter.?table|foreign.?key|index|constraint)",
        min_tier=1, label="data-migration",
    ),
    EscalationPattern(
        pattern=r"(security|vulnerab|injection|xss|csrf|sanitiz|escap)",
        min_tier=1, label="security",
    ),
    EscalationPattern(
        pattern=r"(architect|system.?design|infrastructure|scaling|distributed)",
        min_tier=1, label="architecture",
    ),
]
# fmt: on


def scan(
    task: str,
    patterns: Sequence[EscalationPattern] | None = None,
) -> tuple[int | None, str | None]:
    """Scan task text for escalation patterns.

    Returns ``(min_tier, label)`` for the highest-priority match, or
    ``(None, None)`` if no pattern matches.
    """
    if patterns is None:
        patterns = DEFAULT_PATTERNS

    best_tier: int | None = None
    best_label: str | None = None

    for pat in patterns:
        if re.search(pat.pattern, task, re.IGNORECASE) and (
            best_tier is None or pat.min_tier < best_tier
        ):
            best_tier = pat.min_tier
            best_label = pat.label

    return best_tier, best_label
