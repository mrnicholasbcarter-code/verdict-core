"""Model capability auto-classification by ID pattern matching."""

from __future__ import annotations

import re
from functools import lru_cache

# Tier 0 = most capable (never used for cheap work)
# Tier 3 = cheapest/fastest (only used for low-criticality)
CAPABILITY_PATTERNS: dict[int, list[str]] = {
    # Tier 0 is also the frontier spend guard in the orchestration ladder
    # (verdict/orchestration/eligibility.py _task_gate).
    0: [
        r"opus",
        r"fable",
        r"^gpt-6(?:\.1)?-sol(?:-(?:low|medium|high|xhigh|max|thinking))?$",
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
        r"^gemini-(?:2\.5|3(?:\.1)?)-pro(?:-(?:preview|low|medium|high|xhigh|thinking|latest))*$",
        # BOD-419: strong free families from the live /api/free-models
        # snapshot. Tier 0 stays the frontier spend guard, so these land at
        # tier 1 (sonnet-4/5 / deepseek-r1 / qwen-235b class), not tier 0.
        r"glm-(?:4\.[5-9]|5)",  # Z.ai GLM 4.5+/5.x (incl. zai- vendor ids)
        r"kimi-k\d",  # Moonshot Kimi K2/K3 agentic coding series
        r"deepseek-v4",  # DeepSeek V4 flagship (R1 successor)
        r"deepseek-reasoner",  # DeepSeek R1 API alias
        r"minimax-m\d",  # MiniMax M2/M3 agentic series
        r"qwen[\d.]*-max",  # Qwen Max flagship line
        r"qwen[\d.]*-coder-(?:480b|plus|next)",  # flagship Qwen Coder line
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
        # BOD-419 free families, mid tier (deepseek-v3 / mistral-large /
        # qwen-72b class). Size lookaheads keep explicit cheap sizes (7b,
        # 128b, ...) on the tier-3 markers they already match.
        r"deepseek-(?:chat|3\.2)",  # DeepSeek chat class
        r"deepseek-r1.*distill",  # R1 distills into mid-size open models
        r"qwen2\.5-coder-32b",  # 32B Qwen Coder
        r"qwq",  # QwQ reasoning models
        r"qwen[\d.]*-32b",
        r"qwen[\d.]*-122b",
        r"qwen[\d.]*-next",  # Qwen3-Next efficient line
        r"qwen[\d.]*-(?:vl-|omni-)?plus",  # mid serving tier (below max)
        r"gpt-oss-120b",  # 120B open model, below the sonnet/4o cohort
        r"mistral-medium(?![\w.-]*\d+b)",  # mid Mistral line
        r"codestral(?![\w.-]*\d+b)",  # ~22B Mistral code model
        r"devstral",  # ~24B Mistral coder
        r"gemma.*31b",  # Gemma 31B flagship
    ],
    3: [
        r"haiku",
        r"flash",
        r"(?:^|[-_])mini(?:$|[-_])",  # "minimax" is a model family, not a mini variant
        r"8b",
        r"7b",
        r"nano",
        r"lite",
        r"instant",
        r"smol",
        r"tiny",
        r"phi-4",
        r"gemma.*2b",
        # BOD-419 free families, cheapest tier (small sizes / fast serving
        # tiers / small-active MoE markers, in the spirit of the existing
        # 7b/8b/mini markers).
        r"gpt-oss(?:-safeguard)?-20b",  # 20B small open model
        r"mistral-small",  # small Mistral line
        r"ministral",  # Mistral's small models
        r"qwen[\d.]*-turbo",  # fast/cheap serving tier
        r"qwen[\d.]*-14b",
        r"gemma.*9b",  # Gemma 2 9B
        r"-a[1-4]b",  # small-active MoE suffix (a3b/a4b)
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
    return _classify_raw(model_id.split("/", 1)[-1].lower())


@lru_cache(maxsize=16384)
def _classify_raw(raw: str) -> int:
    """Pattern tier for a lower-cased model id without its provider prefix.

    Pure and cached: a large catalog asks for the same ids many times.
    """

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
    if not _known_raw(raw):
        return None
    return _classify_raw(raw)


@lru_cache(maxsize=16384)
def _known_raw(raw: str) -> bool:
    return any(_matches_any(raw, patterns) for patterns in CAPABILITY_PATTERNS.values())


_COMPILED: dict[tuple[str, ...], tuple[re.Pattern[str], ...]] = {}


def _matches_any(raw: str, patterns: list[str]) -> bool:
    key = tuple(patterns)
    compiled = _COMPILED.get(key)
    if compiled is None:
        compiled = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        _COMPILED[key] = compiled
    return any(p.search(raw) for p in compiled)
