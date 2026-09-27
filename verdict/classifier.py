"""Model capability auto-classification by ID pattern matching."""

from __future__ import annotations

import re

# Tier 0 = most capable (never used for cheap work)
# Tier 3 = cheapest/fastest (only used for low-criticality)
CAPABILITY_PATTERNS: dict[int, list[str]] = {
    # Current frontier families match the repo's own frontier markers
    # (verdict/orchestration/eligibility.py _FRONTIER_MARKERS).
    0: [
        r"opus",
        r"fable",
        r"gpt-6-sol",
        r"gpt-6-astra",
        r"gpt-5\.6(?!-luna)",
        r"gpt-5\.5",
        r"grok-4",
        r"o3-pro",
        r"o3(?!-mini)",
    ],
    1: [
        r"sonnet-5",
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
        r"mini(?!max)",  # "minimax" is a model family, not a mini variant
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

    # Cheap-variant markers (mini / nano / flash / lite / haiku / …) take
    # precedence over frontier family names: "gpt-5.5-mini" or "o3-mini" is a
    # small model, not a tier-0 frontier, even though its family pattern matches
    # (free-tier ordering classifier precedence). Explicit tier-2 rows such as
    # "gpt-4o-mini" still win over the generic tier-3 markers.
    cheap_variant = _matches_any(raw, CAPABILITY_PATTERNS[3])
    for tier in (0, 1):
        if cheap_variant:
            break
        if _matches_any(raw, CAPABILITY_PATTERNS[tier]):
            return tier
    for tier in (2, 3):
        if _matches_any(raw, CAPABILITY_PATTERNS[tier]):
            return tier

    return 2  # default to medium if unknown


def classify_known(model_id: str, overrides: dict[str, int] | None = None) -> int | None:
    """Like :func:`classify`, but ``None`` when no override or pattern matches.

    Assignment (BOD-271) must keep unknown capability explicit instead of
    silently treating an unrecognized model as medium tier.
    """
    if overrides and model_id in overrides:
        return overrides[model_id]
    raw = model_id.split("/", 1)[-1].lower()
    if not any(_matches_any(raw, patterns) for patterns in CAPABILITY_PATTERNS.values()):
        return None
    return classify(model_id, overrides)


def _matches_any(raw: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, raw, re.IGNORECASE) for pattern in patterns)
