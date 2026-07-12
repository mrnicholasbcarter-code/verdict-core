"""Core routing algorithm prioritizing cost, tier, and capability."""

from llm_gate.models import ModelInfo, ProviderConfig


def select_best_model(
    candidates: list[ModelInfo], tier: int, configs: dict[str, ProviderConfig]
) -> tuple[ModelInfo | None, list[str]]:
    """Selects the cheapest adequate model out of candidates.

    Sorts by:
    1. capability_tier descending (cheaper models first, bounded by max allowed tier)
    2. Configured provider priority
    3. Alphabetical model ID for stable fallback
    """
    valid = [m for m in candidates if m.capability_tier <= tier and m.is_available]
    if not valid:
        return None, [m.id for m in candidates]

    # Highest capability_tier integer is cheapest
    valid.sort(key=lambda m: (-m.capability_tier, -configs[m.provider].priority, m.id))

    chosen = valid[0]
    alts = [m.id for m in valid[1:5]]
    return chosen, alts
