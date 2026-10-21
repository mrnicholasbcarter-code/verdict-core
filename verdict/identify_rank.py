"""Rank model routes strongest first from Core's rating evidence (price excluded).

Ratings come from the metadata store (OpenRouter's Artificial Analysis feed):
``aa_coding`` for code work, else ``aa_intelligence``. A route without a fresh
score falls back to a caller-declared capability tier, else it is unknown and
ranks last. Scores and tiers are different scales and are never combined into
one number. Nothing is guessed from the model name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from verdict.metadata.mapping import IdentityMap
from verdict.metadata.records import ModelMetadataError
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id

# Same line the doctor's metadata row draws: older than 12 hours is stale.
RATING_MAX_AGE = timedelta(hours=12)
_CODE_FAMILIES = frozenset({"code_edit", "code_review", "coding", "code"})

RatingBasis = Literal["score", "tier", "unknown"]


@dataclass(frozen=True)
class RatedModel:
    """One route's rating with provenance.

    ``score``/``metric``/``source``/``as_of`` are what the store holds for the
    task metric, even when stale. ``basis`` is what the order used.
    """

    route_id: str
    provider: str
    score: float | None
    metric: str | None
    source: str | None
    as_of: str | None
    tier: int | None = None
    basis: RatingBasis = "unknown"


def rating_metric(task_family: str) -> str:
    """The one AA metric that rates this task family (no cross-metric fallback)."""
    return "aa_coding" if task_family.split(":", 1)[0] in _CODE_FAMILIES else "aa_intelligence"


def _fresh(as_of: str | None, now: datetime) -> bool:
    if not as_of:
        return False
    try:
        fetched = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        return False
    return timedelta(0) <= now - fetched <= RATING_MAX_AGE


def rate_model(
    route_id: str,
    *,
    metadata: MetadataSnapshot | None,
    task_family: str = "code_edit",
    declared_tier: int | None = None,
    identity_map: IdentityMap | None = None,
    now: datetime | None = None,
) -> RatedModel:
    metric = rating_metric(task_family)
    clock = now or datetime.now(timezone.utc)
    score: float | None = None
    source: str | None = None
    as_of: str | None = None
    if metadata is not None:
        try:
            record = lookup_omniroute_id(metadata, route_id, identity_map=identity_map).record
        except ModelMetadataError:
            record = None
        field = getattr(record.scores, metric) if record is not None else None
        if field is not None and not isinstance(field.value, bool):
            score = float(field.value)
            source = field.provenance.source
            as_of = field.provenance.fetched_at
    basis: RatingBasis = "unknown"
    if score is not None and _fresh(as_of, clock):
        basis = "score"
    elif declared_tier is not None:
        basis = "tier"
    return RatedModel(
        route_id=route_id,
        provider=route_id.split("/", 1)[0],
        score=score,
        metric=metric,
        source=source,
        as_of=as_of,
        tier=declared_tier,
        basis=basis,
    )


def rating_key(rated: RatedModel) -> tuple[int, float, int]:
    """Smaller sorts first: fresh score (desc), then declared tier (asc), then unknown."""
    if rated.basis == "score" and rated.score is not None:
        return (0, -rated.score, 0)
    if rated.basis == "tier" and rated.tier is not None:
        return (1, 0.0, rated.tier)
    return (2, 0.0, 0)


def rank_models(
    route_ids: Sequence[str],
    *,
    metadata: MetadataSnapshot | None,
    task_family: str = "code_edit",
    declared_tiers: Mapping[str, int] | None = None,
    identity_map: IdentityMap | None = None,
    now: datetime | None = None,
) -> list[RatedModel]:
    """Rated rows strongest first; unknown last; ``route_id`` breaks ties."""
    clock = now or datetime.now(timezone.utc)
    tiers = declared_tiers or {}
    rows = [
        rate_model(
            route_id,
            metadata=metadata,
            task_family=task_family,
            declared_tier=tiers.get(route_id),
            identity_map=identity_map,
            now=clock,
        )
        for route_id in dict.fromkeys(route_ids)
    ]
    return sorted(rows, key=lambda row: (*rating_key(row), row.route_id))
