"""Model capability auto-classification by ID pattern matching."""

from __future__ import annotations

import re

# Tier 0 = most capable (never used for cheap work)
# Tier 3 = cheapest/fastest (only used for low-criticality)
CAPABILITY_PATTERNS: dict[int, list[str]] = {
    0: [r"opus", r"gpt-5\.5", r"grok-4", r"o3-pro", r"o3(?!-mini)"],
    1: [
        r"sonnet-4",
        r"gpt-5\.4",
        r"gpt-4o(?!-mini)",
        r"grok-3",
        r"claude-3\.5-sonnet",
        r"deepseek-r1(?!.*distill)",
        r"qwen.*235b",
    ],
    2: [
        r"sonnet-3",
        r"gpt-4o-mini",
        r"haiku-3\.5",
        r"llama.*70b",
        r"qwen.*72b",
        r"mistral-large",
        r"deepseek-v3",
        r"command-r-plus",
    ],
    3: [
        r"haiku",
        r"flash",
        r"mini",
        r"8b",
        r"7b",
        r"nano",
        r"lite",
        r"instant",
        r"smol",
        r"tiny",
        r"phi-4",
        r"gemma.*2b",
    ],
}


def classify(model_id: str, overrides: dict[str, int] | None = None) -> int:
    """Classify a model ID into a capability tier (0-3).

    Checks ``overrides`` first (exact match on full ID), then falls back to
    pattern matching. Returns 2 (medium) if no pattern matches.
    """
    if overrides and model_id in overrides:
        return overrides[model_id]

    # Strip provider prefix for matching (e.g., "anthropic/claude-sonnet-4" → "claude-sonnet-4")
    raw = model_id.split("/", 1)[-1].lower()

    for tier in range(4):
        for pattern in CAPABILITY_PATTERNS[tier]:
            if re.search(pattern, raw, re.IGNORECASE):
                return tier

    return 2  # default to medium if unknown
