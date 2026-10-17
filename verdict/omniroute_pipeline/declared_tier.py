"""Phase g (R5): declared capability tiers from S11 Elo, written at admit time.

O3 (controller 10:05Z): tier 1 = Elo >= 1450, tier 2 = 1380-1449, below that the
pattern classifier decides, and never tier 0 by declaration: tier 0 comes only
from the pattern table. Dark unless ``VERDICT_PIPELINE_RANKING_MODE=write``;
route Elo is read from ``declared-tiers.json`` (written by the phase d
ranking ingest), so with no file the admit tier is exactly ``classify``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

RANKING_MODE_ENV = "VERDICT_PIPELINE_RANKING_MODE"
DECLARED_TIERS_NAME = "declared-tiers.json"
TIER1_MIN_ELO = 1450
TIER2_MIN_ELO = 1380


def ranking_write_enabled() -> bool:
    return os.getenv(RANKING_MODE_ENV, "").strip().lower() == "write"


def declared_tier_from_elo(elo: float | None) -> int | None:
    if elo is None:
        return None
    if elo >= TIER1_MIN_ELO:
        return 1
    if elo >= TIER2_MIN_ELO:
        return 2
    return None


def default_declared_tiers_path() -> Path:
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent / DECLARED_TIERS_NAME


def load_declared_tiers(path: Path | None = None) -> dict[str, int]:
    """Exact route id -> declared tier from the S11 Elo file; empty unless write mode.

    The file maps route ids to text Elo, so a tier is always derived through the
    O3 bands and a tier 0 can never be declared.
    """
    if not ranking_write_enabled():
        return {}
    try:
        data = json.loads((path or default_declared_tiers_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for route, elo in data.items():
        if isinstance(elo, bool) or not isinstance(elo, int | float):
            continue
        tier = declared_tier_from_elo(float(elo))
        if tier is not None:
            out[str(route)] = tier
    return out


def resolve_capability_tier(model_id: str, declared: Mapping[str, int]) -> int:
    """Pattern tier 0 always wins; otherwise a declared 1/2 tier, else the pattern."""
    from verdict.classifier import classify

    pattern = classify(model_id)
    if pattern == 0:
        return 0
    tier = declared.get(model_id)
    return tier if tier in (1, 2) else pattern
