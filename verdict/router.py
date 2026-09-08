"""Core routing algorithm prioritizing quality, tier, and capability."""

from verdict.eligibility import EligibilityResult
from verdict.models import ModelInfo, ProviderConfig


def select_best_model(
    candidates: list[ModelInfo], tier: int, configs: dict[str, ProviderConfig]
) -> tuple[ModelInfo | None, list[str]]:
    """Select the highest-quality eligible model out of candidates.

    The selector is deterministic and only considers candidates that satisfy the
    requested tier and are currently available. Quality confidence wins first,
    then capability tier, provider priority, and stable model ID.
    """
    valid = [
        model
        for model in candidates
        if model.capability_tier <= tier
        and model.is_available
        and model.availability_state in {"eligible", "ready"}
    ]
    if not valid:
        return None, [m.id for m in candidates]

    valid.sort(
        key=lambda model: (
            -(
                model.quality_confidence
                if model.quality_confidence is not None
                else max(0.0, 1.0 - model.capability_tier / 3.0)
            ),
            model.capability_tier,
            -configs.get(model.provider, ProviderConfig()).priority,
            model.id,
        )
    )

    chosen = valid[0]
    alts = [m.id for m in valid[1:5]]
    return chosen, alts


def select_best_eligible_model(
    eligibility: EligibilityResult, tier: int, configs: dict[str, ProviderConfig]
) -> tuple[ModelInfo | None, list[str]]:
    """Rank only the authoritative set produced by :class:`EligibilityGate`."""
    if not isinstance(eligibility, EligibilityResult):
        raise TypeError("eligibility must be an EligibilityResult")
    return select_best_model(eligibility.eligible, tier, configs)
