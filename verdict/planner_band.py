"""Planner band selection from live ratings (ADR-051).

The frontier band is the top ``VERDICT_PLANNER_FRONTIER_TOP`` fraction (default 0.20)
of distinct rated models by fresh ``aa_intelligence`` among the rated models in
Verdict's route inventory.  The fallback band is the top
``VERDICT_PLANNER_FALLBACK_TOP`` fraction (default 0.30).  Both read only the
newest ``fetched_at`` batch of the rating source; a score from an older batch
is unrated even if it is still within the 12-hour freshness window.

Selection narrows the already-admitted planner candidates to the frontier band,
else to the fallback band.  It never adds a candidate that failed admission.
METERED routes are excluded until CP1 supplies a spend limit (ADR-051 D5).
UNKNOWN counts as non-metered (ADR-051 D10, ADR-049).

Recorded in every selection:
  planner_band = {
      required: "frontier",
      used:     "frontier" | "fallback",
      reason:   str | None,      # None when used == required
      rank:     int,              # 1-indexed rank among all rated models
      rated_models: int,          # total distinct rated models in inventory
      cutoff:   float,            # cutoff score for the used band
      rating:   float,            # the route's aa_intelligence score
      metric:   "aa_intelligence",
      fetched_at: str,            # ISO timestamp of the score batch
  }
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from verdict.metadata.mapping import IdentityMap
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id
from verdict.orchestration.contracts import CapacityClass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENV_FRONTIER = "VERDICT_PLANNER_FRONTIER_TOP"
_ENV_FALLBACK = "VERDICT_PLANNER_FALLBACK_TOP"
_DEFAULT_FRONTIER = 0.20
_DEFAULT_FALLBACK = 0.30

_METRIC = "aa_intelligence"
_TASK_FAMILY = "specification"


class PlannerBandConfigError(ValueError):
    """Named error for invalid VERDICT_PLANNER_FRONTIER_TOP or VERDICT_PLANNER_FALLBACK_TOP."""


@dataclass(frozen=True)
class PlannerBandConfig:
    """Validated band fractions read from environment variables."""

    frontier_top: float  # 0 < frontier_top <= fallback_top <= 1
    fallback_top: float


def load_planner_band_config() -> PlannerBandConfig:
    """Read band fractions from env, validate, and return a config.

    Raises :class:`PlannerBandConfigError` with a named error message on any
    invalid value (non-numeric, out-of-range, or frontier > fallback).
    """
    raw_frontier = os.environ.get(_ENV_FRONTIER, "")
    raw_fallback = os.environ.get(_ENV_FALLBACK, "")

    frontier = (
        _DEFAULT_FRONTIER if not raw_frontier else _parse_fraction(raw_frontier, _ENV_FRONTIER)
    )
    fallback = (
        _DEFAULT_FALLBACK if not raw_fallback else _parse_fraction(raw_fallback, _ENV_FALLBACK)
    )

    if frontier <= 0:
        raise PlannerBandConfigError(
            f"invalid_planner_band_config: {_ENV_FRONTIER}={raw_frontier!r} must be > 0"
        )
    if fallback <= 0:
        raise PlannerBandConfigError(
            f"invalid_planner_band_config: {_ENV_FALLBACK}={raw_fallback!r} must be > 0"
        )
    if frontier > 1:
        raise PlannerBandConfigError(
            f"invalid_planner_band_config: {_ENV_FRONTIER}={raw_frontier!r} must be <= 1"
        )
    if fallback > 1:
        raise PlannerBandConfigError(
            f"invalid_planner_band_config: {_ENV_FALLBACK}={raw_fallback!r} must be <= 1"
        )
    if frontier > fallback:
        raise PlannerBandConfigError(
            f"invalid_planner_band_config: {_ENV_FRONTIER}={frontier} > {_ENV_FALLBACK}={fallback}"
        )
    return PlannerBandConfig(frontier_top=frontier, fallback_top=fallback)


def _parse_fraction(value: str, name: str) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise PlannerBandConfigError(
            f"invalid_planner_band_config: {name}={value!r} is not a number"
        ) from exc
    if math.isnan(result):
        raise PlannerBandConfigError(f"invalid_planner_band_config: {name}={value!r} is NaN")
    return result


# ---------------------------------------------------------------------------
# Band computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandComputation:
    """Result of computing the frontier and fallback bands from the route inventory.

    ``frontier_model_keys`` and ``fallback_model_keys`` are sets of stripped model
    keys (route ID with any trailing ``:suffix`` removed).  To check whether an
    admitted route is in a band, strip its suffix and look it up here.

    The cutoff score is the lowest aa_intelligence score that qualified for that
    band.  ``rated_models`` is the total number of distinct rated models in the
    population (the inventory routes with fresh newest-batch scores).
    """

    frontier_model_keys: frozenset[str]
    fallback_model_keys: frozenset[str]
    cutoff_frontier: float
    cutoff_fallback: float
    rated_models: int
    fetched_at: str
    model_scores: dict[str, float]  # model_key -> aa_intelligence score
    model_ranks: dict[str, int]  # model_key -> 1-indexed rank


def _strip_suffix(route_id: str) -> str:
    """Strip a trailing ``:suffix`` from a route id.

    ``cc/claude:free``  → ``cc/claude``
    ``oa/gpt-4:nitro``  → ``oa/gpt-4``
    ``cc/claude``       → ``cc/claude``
    """
    if ":" in route_id:
        return route_id.rsplit(":", 1)[0]
    return route_id


def _score_field(record: Any, metric: str) -> tuple[float | None, str | None]:
    """Return (score, fetched_at) for ``metric`` on ``record``, or (None, None)."""
    scores = getattr(record, "scores", None)
    field = getattr(scores, metric, None) if scores is not None else None
    if field is not None and not isinstance(field.value, bool):
        value = field.value
        fetched = field.provenance.fetched_at if field.provenance else None
        try:
            return float(value), fetched
        except (TypeError, ValueError):
            pass
    return None, None


def compute_planner_bands(
    inventory_route_ids: Sequence[str],
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    *,
    config: PlannerBandConfig,
    now: datetime,
) -> BandComputation:
    """Compute the frontier and fallback bands from the route inventory.

    Population: rated models in ``inventory_route_ids``, using only the newest
    ``fetched_at`` batch of the ``aa_intelligence`` source.  Suffix variants and
    aliases of one metadata record share one slot.

    Returns a :class:`BandComputation` with the band membership sets and cutoffs.
    If ``metadata`` is ``None`` or no routes have a fresh newest-batch
    ``aa_intelligence`` score, both band sets are empty.
    """
    from verdict.identify_rank import _fresh

    if metadata is None:
        return BandComputation(
            frontier_model_keys=frozenset(),
            fallback_model_keys=frozenset(),
            cutoff_frontier=0.0,
            cutoff_fallback=0.0,
            rated_models=0,
            fetched_at="",
            model_scores={},
            model_ranks={},
        )

    # Step 1: strip suffix and dedup; map stripped_route → representative route
    # We use the stripped route as the model key; multiple suffixed variants
    # collapse to their stripped form.
    stripped_to_routes: dict[str, list[str]] = {}
    for route_id in inventory_route_ids:
        stripped = _strip_suffix(str(route_id))
        stripped_to_routes.setdefault(stripped, []).append(str(route_id))

    # Step 2: for each distinct stripped route, look up its metadata record
    # and get its aa_intelligence score + fetched_at.
    # Group by resolved metadata record ID to handle aliases.
    record_to_model_key: dict[str, str] = {}  # record.id → first model_key seen
    record_to_alias_keys: dict[str, list[str]] = {}  # record.id → all model_keys
    model_key_to_score: dict[str, float] = {}
    model_key_to_fetched: dict[str, str] = {}

    for model_key in sorted(stripped_to_routes):
        try:
            lu = lookup_omniroute_id(metadata, model_key, identity_map=identity_map)
        except ModelMetadataError:
            continue
        if lu.record is None:
            continue
        record_id = lu.record.id
        # Track all model_keys for this record, for alias expansion in step 6
        record_to_alias_keys.setdefault(record_id, []).append(model_key)
        # If this record was already seen under a different model_key, skip
        # (the first model_key alphabetically wins, since we sorted above)
        if record_id in record_to_model_key:
            # Alias: de-duplicate for band sizing; aliases are expanded in step 6
            continue
        score, fetched = _score_field(lu.record, _METRIC)
        if score is None or fetched is None:
            continue
        # Only fresh scores (within 12h of now)
        if not _fresh(fetched, now):
            continue
        record_to_model_key[record_id] = model_key
        model_key_to_score[model_key] = score
        model_key_to_fetched[model_key] = fetched

    if not model_key_to_score:
        return BandComputation(
            frontier_model_keys=frozenset(),
            fallback_model_keys=frozenset(),
            cutoff_frontier=0.0,
            cutoff_fallback=0.0,
            rated_models=0,
            fetched_at="",
            model_scores={},
            model_ranks={},
        )

    # Step 3: newest fetch only — keep only scores from the max fetched_at batch
    max_fetched = max(model_key_to_fetched.values())
    population: list[tuple[float, str]] = [
        (score, key)
        for key, score in model_key_to_score.items()
        if model_key_to_fetched[key] == max_fetched
    ]

    if not population:
        return BandComputation(
            frontier_model_keys=frozenset(),
            fallback_model_keys=frozenset(),
            cutoff_frontier=0.0,
            cutoff_fallback=0.0,
            rated_models=0,
            fetched_at=max_fetched,
            model_scores={},
            model_ranks={},
        )

    # Step 4: sort descending by score, then by model_key (lexicographic tiebreak)
    population.sort(key=lambda t: (-t[0], t[1]))
    n = len(population)

    # Step 5: compute band cutoffs (ceil fractions; include all ties at cutoff)
    frontier_size = math.ceil(n * config.frontier_top)
    fallback_size = math.ceil(n * config.fallback_top)

    # Cutoff = score of the last model in the ceil(n*frac) slice
    cutoff_frontier = population[frontier_size - 1][0] if frontier_size > 0 else 0.0
    cutoff_fallback = population[fallback_size - 1][0] if fallback_size > 0 else 0.0

    # Include all models tied with the cutoff score
    frontier_keys_set = {key for score, key in population if score >= cutoff_frontier}
    fallback_keys_set = {key for score, key in population if score >= cutoff_fallback}

    # Build score and rank maps
    scores_map = {key: score for score, key in population}
    ranks_map: dict[str, int] = {}
    rank = 1
    for i, (score, key) in enumerate(population):
        if i > 0 and score < population[i - 1][0]:
            rank = i + 1
        ranks_map[key] = rank

    # Step 6: expand bands and score/rank maps to include all aliases of each model
    # (all inventory routes that resolve to the same metadata record as a band member)
    for record_id, alias_keys in record_to_alias_keys.items():
        first_key = record_to_model_key.get(record_id)
        if first_key is None or first_key not in scores_map:
            continue  # record not in population (unrated or stale)
        for alias_key in alias_keys:
            if alias_key == first_key:
                continue
            scores_map[alias_key] = scores_map[first_key]
            ranks_map[alias_key] = ranks_map[first_key]
            if first_key in frontier_keys_set:
                frontier_keys_set.add(alias_key)
            if first_key in fallback_keys_set:
                fallback_keys_set.add(alias_key)

    frontier_keys = frozenset(frontier_keys_set)
    fallback_keys = frozenset(fallback_keys_set)

    return BandComputation(
        frontier_model_keys=frontier_keys,
        fallback_model_keys=fallback_keys,
        cutoff_frontier=cutoff_frontier,
        cutoff_fallback=cutoff_fallback,
        rated_models=n,
        fetched_at=max_fetched,
        model_scores=scores_map,
        model_ranks=ranks_map,
    )


# ---------------------------------------------------------------------------
# Selection result
# ---------------------------------------------------------------------------

PlannerBandLabel = Literal["frontier", "fallback"]


@dataclass(frozen=True)
class PlannerBandResult:
    """Selection metadata to record per planner in the run report and receipt."""

    required: str  # always "frontier"
    used: str  # "frontier" | "fallback"
    reason: str | None  # None when used == required
    rank: int  # 1-indexed rank among all rated models
    rated_models: int
    cutoff: float  # cutoff score for the used band
    rating: float  # the route's aa_intelligence score
    metric: str  # "aa_intelligence"
    fetched_at: str  # ISO timestamp of the score batch

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "used": self.used,
            "reason": self.reason,
            "rank": self.rank,
            "rated_models": self.rated_models,
            "cutoff": self.cutoff,
            "rating": self.rating,
            "metric": self.metric,
            "fetched_at": self.fetched_at,
        }


class PlannerBandError(ValueError):
    """Fail-closed error when no admissible route is in either planner band."""


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

#: Non-metered capacity classes for planner selection (ADR-051 D5, D10).
#: METERED excluded until CP1.  UNKNOWN counts as non-metered (ADR-049).
_NON_METERED = frozenset({CapacityClass.FREE, CapacityClass.SUBSCRIPTION, CapacityClass.UNKNOWN})

#: Class order within non-metered: FREE/UNKNOWN before SUBSCRIPTION (ADR-051 D14).
_CLASS_ORDER: dict[CapacityClass, int] = {
    CapacityClass.FREE: 0,
    CapacityClass.UNKNOWN: 0,
    CapacityClass.SUBSCRIPTION: 1,
    CapacityClass.METERED: 2,
}


def _aa_coding_score(
    route_id: str,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    now: datetime,
) -> float:
    """Return the route's fresh aa_coding score, or 0.0 if absent/stale."""
    if metadata is None:
        return 0.0
    stripped = _strip_suffix(route_id)
    try:
        lu = lookup_omniroute_id(metadata, stripped, identity_map=identity_map)
    except ModelMetadataError:
        return 0.0
    if lu.record is None:
        return 0.0
    score, fetched = _score_field(lu.record, "aa_coding")
    if score is None or fetched is None:
        return 0.0
    from verdict.identify_rank import _fresh

    return score if _fresh(fetched, now) else 0.0


def _sort_key(
    route_id: str,
    capacity_class: CapacityClass,
    band: BandComputation,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    now: datetime,
) -> tuple[int, float, int, float, str]:
    """Sort key for ordering candidates within a band.

    Smaller sorts better (ascending), per ADR-051 D5/D14:
    - 0 = non-metered, 1 = metered  (METERED excluded but kept last in key)
    - rating descending (negated)  ← D5: highest-rated non-metered wins
    - cost class order (FREE/UNKNOWN=0, SUBSCRIPTION=1)  ← D14: equal score → FREE first
    - aa_coding descending (negated)  ← D14: then higher aa_coding
    - route_id lexicographic tiebreak
    """
    is_metered = 0 if capacity_class in _NON_METERED else 1
    model_key = _strip_suffix(route_id)
    score = band.model_scores.get(model_key, 0.0)
    cls_order = _CLASS_ORDER.get(capacity_class, 2)
    aa_coding = _aa_coding_score(route_id, metadata, identity_map, now)
    return (is_metered, -score, cls_order, -aa_coding, route_id)


def _band_result(
    route_id: str, used: str, reason: str | None, band: BandComputation, used_cutoff: float
) -> PlannerBandResult:
    """Build a PlannerBandResult for the selected route."""
    model_key = _strip_suffix(route_id)
    score = band.model_scores.get(model_key, 0.0)
    rank = band.model_ranks.get(model_key, 0)
    return PlannerBandResult(
        required="frontier",
        used=used,
        reason=reason,
        rank=rank,
        rated_models=band.rated_models,
        cutoff=used_cutoff,
        rating=score,
        metric=_METRIC,
        fetched_at=band.fetched_at,
    )


def _filter_to_band(
    admitted: Sequence[str],
    model_keys: frozenset[str],
    capacity_classes: Mapping[str, CapacityClass],
) -> list[str]:
    """Return admitted routes in ``model_keys`` with non-metered capacity class."""
    result = []
    for route_id in admitted:
        model_key = _strip_suffix(route_id)
        if model_key not in model_keys:
            continue
        cap = capacity_classes.get(route_id, CapacityClass.UNKNOWN)
        if cap not in _NON_METERED:
            continue  # METERED excluded until CP1
        result.append(route_id)
    return result


def select_planner_routes(
    admitted_route_ids: Sequence[str],
    capacity_classes: Mapping[str, CapacityClass],
    band: BandComputation,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    *,
    top_n: int = 1,
    now: datetime | None = None,
) -> list[tuple[str, PlannerBandResult]]:
    """Select up to ``top_n`` planner routes from admitted candidates using the band.

    Uses the frontier band first, falls back to the fallback band.  Applies the
    two-family rule (at least two route families across all selected routes; each
    failover uses the best available band for a NEW family).  Raises
    :class:`PlannerBandError` with ``no_frontier_or_fallback_planner`` when no
    admissible route is in either band, or when fewer than 2 families are
    admissible (and ``top_n >= 2``).

    Returns a list of ``(route_id, PlannerBandResult)`` in priority order.
    """
    from verdict.orchestration.contracts import route_family

    clock = now or datetime.now(timezone.utc)

    # Candidates in frontier band
    frontier_candidates = _filter_to_band(
        admitted_route_ids, band.frontier_model_keys, capacity_classes
    )
    # Candidates in fallback band (strict: only those NOT already in frontier)
    fallback_only_candidates = _filter_to_band(
        [r for r in admitted_route_ids if r not in set(frontier_candidates)],
        band.fallback_model_keys,
        capacity_classes,
    )

    # Sort each group
    def _key(route_id: str) -> tuple[int, float, int, float, str]:
        cap = capacity_classes.get(route_id, CapacityClass.UNKNOWN)
        return _sort_key(route_id, cap, band, metadata, identity_map, clock)

    frontier_candidates.sort(key=_key)
    fallback_only_candidates.sort(key=_key)

    all_candidates_in_order = [
        (r, "frontier", None, band.cutoff_frontier) for r in frontier_candidates
    ] + [
        (r, "fallback", "no_frontier_admissible", band.cutoff_fallback)
        for r in fallback_only_candidates
    ]

    if not all_candidates_in_order:
        raise PlannerBandError(
            "no_frontier_or_fallback_planner: no admissible route is in the frontier or fallback band"
        )

    selected: list[tuple[str, PlannerBandResult]] = []
    used_families: set[str] = set()

    for route_id, used, reason, cutoff in all_candidates_in_order:
        family = route_family(route_id)
        if family in used_families:
            continue
        result = _band_result(route_id, used, reason, band, cutoff)
        selected.append((route_id, result))
        used_families.add(family)
        if len(selected) == top_n:
            break

    if top_n >= 2 and len(selected) < 2:
        family_count = len(used_families) if selected else 0
        raise PlannerBandError(
            f"no_frontier_or_fallback_planner: fewer than 2 admissible families found "
            f"({family_count} family/families across both bands)"
        )

    return selected


# ---------------------------------------------------------------------------
# Doctor / identify helpers
# ---------------------------------------------------------------------------


def planner_supply_status(
    admitted_route_ids: Sequence[str],
    capacity_classes: Mapping[str, CapacityClass],
    band: BandComputation,
) -> dict[str, Any]:
    """Return a doctor/identify summary of planner band supply.

    Keys:
    - ``frontier_admissible``: number of admissible routes in the frontier band
    - ``fallback_admissible``: number of admissible routes in the fallback band
    - ``warning``: True when no admissible route is in the frontier band
    - ``message``: human-readable supply summary for doctor sections
    """
    frontier = _filter_to_band(admitted_route_ids, band.frontier_model_keys, capacity_classes)
    # fallback_model_keys ⊇ frontier_model_keys, so this already includes frontier routes
    fallback = _filter_to_band(admitted_route_ids, band.fallback_model_keys, capacity_classes)
    fallback_total = len(fallback)
    has_frontier = len(frontier) > 0
    has_fallback = fallback_total > 0
    warning = not has_frontier
    if not warning:
        msg = f"ok: {len(frontier)} frontier-band route(s), {band.rated_models} rated models"
    elif has_fallback:
        msg = (
            f"warn: no frontier-band planner; fallback band reachable "
            f"({fallback_total} route(s)); {band.rated_models} rated models"
        )
    else:
        msg = (
            f"warn: no frontier-band planner; fallback band empty; {band.rated_models} rated models"
        )
    return {
        "frontier_admissible": len(frontier),
        "fallback_admissible": fallback_total,
        "warning": warning,
        "message": msg,
    }


def rating_coverage_summary(
    inventory_route_ids: Sequence[str],
    band: BandComputation,
    metadata: MetadataSnapshot | None,
    identity_map: IdentityMap | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Return identify-level coverage summary for planner band metadata.

    Keys:
    - ``rated_models``: number of distinct models with a fresh newest-batch score
    - ``inventory_routes``: total inventory routes examined
    - ``coverage_pct``: rated_models / inventory_routes * 100 (rounded to 1 dp)
    - ``frontier_model_count``: number of models in the frontier band
    - ``fallback_model_count``: number of models in the fallback band (cumulative)
    - ``cutoff_frontier``: cutoff score for the frontier band
    - ``cutoff_fallback``: cutoff score for the fallback band
    """
    n_inv = len(list(inventory_route_ids))
    n_rated = band.rated_models
    pct = round(100.0 * n_rated / n_inv, 1) if n_inv > 0 else 0.0
    return {
        "rated_models": n_rated,
        "inventory_routes": n_inv,
        "coverage_pct": pct,
        "frontier_model_count": len(band.frontier_model_keys),
        "fallback_model_count": len(band.fallback_model_keys),
        "cutoff_frontier": band.cutoff_frontier,
        "cutoff_fallback": band.cutoff_fallback,
    }


def planner_band_families(
    band: BandComputation, metadata: MetadataSnapshot | None, identity_map: IdentityMap | None
) -> list[str]:
    """Return the distinct provider families of models in the frontier band.

    The family is the provider prefix (the part before '/') of the model key.
    """
    families: set[str] = set()
    for model_key in band.frontier_model_keys:
        family = model_key.split("/", 1)[0] if "/" in model_key else model_key
        families.add(family)
    return sorted(families)
