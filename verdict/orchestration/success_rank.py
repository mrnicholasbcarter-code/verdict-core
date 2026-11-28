"""Verified success score for ranking admitted routes (GitHub issue #1216, slice 1).

The score reorders routes the ladder has already admitted. It never admits one: a route
with too few verified results is neutral, and everything here reads only the session
ledger's controller-verified rows of real work.

The score is :func:`verdict.strength_profiles.score_task_history` (smoothed toward 0.5,
7-day half-life), fed from ledger rows; there is no second formula. A route is scored by
its own results for the node's kind (``route+kind``); with fewer than ``min_sample`` of
them it falls back to its results for all kinds (``route``); with fewer than that it is
neutral. Aliases of one model in one backend pool share a record, as in the gate.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from verdict.candidate_pool import OutcomeObservation
from verdict.orchestration.session_evidence import SessionOutcome, pool_key
from verdict.strength_profiles import score_task_history

SUCCESS_WINDOW_DAYS = 14
# Matches the nearest existing precedent, the history scorer's demotion floor.
SUCCESS_MIN_SAMPLE = 3
# ``score_task_history`` ignores anything older than this, so a longer window is a lie.
MAX_SUCCESS_WINDOW_DAYS = 30
# The sort key sees the score in tenths so that a day's decay between two routes with the
# same record never outranks slack; the receipt keeps the exact score.
ORDER_STEPS = 10
NEUTRAL_SCORE = 0.5

_ALL_KINDS = "*"

PoolIndex = Mapping[tuple[str, str], Sequence[SessionOutcome]]


@dataclass(frozen=True)
class SuccessRank:
    """One route's success evidence for one node kind, as the receipt records it.

    ``score`` and ``n`` are None when no evidence could be read (``source`` says why);
    ``key`` is None when no key had ``min_sample`` results, so the route is neutral.
    """

    score: float | None
    n: int | None
    key: str | None
    node_kind: str | None
    source: str
    window_days: int
    min_sample: int

    @property
    def order(self) -> int:
        """Sort value, smaller ranks first: neutral for every unscored route."""
        return -round((NEUTRAL_SCORE if self.score is None else self.score) * ORDER_STEPS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "n": self.n,
            "key": self.key,
            "node_kind": self.node_kind,
            "source": self.source,
            "window_days": self.window_days,
            "min_sample": self.min_sample,
        }


def unavailable(
    reason: str, node_kind: str | None, window_days: int, min_sample: int
) -> SuccessRank:
    """Neutral rank with a named reason: no ledger, no node kind, or an unreadable ledger."""
    return SuccessRank(None, None, None, node_kind, reason, window_days, min_sample)


def index_outcomes(outcomes: Iterable[SessionOutcome]) -> PoolIndex:
    """Group outcomes by backend pool and model so each route is one lookup."""
    grouped: defaultdict[tuple[str, str], list[SessionOutcome]] = defaultdict(list)
    for item in outcomes:
        grouped[pool_key(item.route_id)].append(item)
    return grouped


def _observations(
    route_id: str, family: str, items: Iterable[SessionOutcome]
) -> list[OutcomeObservation]:
    # A false claim counts once here (the gate's summary counts it twice); the gate still
    # vetoes a recent false claim, so the rank needs no second penalty.
    return [
        OutcomeObservation(
            route_id=route_id,
            task_family=family,
            success=item.outcome == "pass",
            observed_at=item.at,
            quality_credit=1.0 if item.outcome == "pass" else 0.0,
            observation_id=f"{item.route_id}|{item.at.isoformat()}|{item.outcome}",
        )
        for item in items
    ]


def rank_success(
    index: PoolIndex,
    route_id: str,
    node_kind: str,
    now: datetime,
    *,
    window_days: int,
    min_sample: int,
) -> SuccessRank:
    """Score ``route_id`` for ``node_kind``, falling back to the route alone, then neutral."""
    mine = index.get(pool_key(route_id), ())
    of_kind = [item for item in mine if item.node_kind == node_kind]
    n_all = 0
    for key, family, items in (
        ("route+kind", node_kind, of_kind),
        ("route", _ALL_KINDS, list(mine)),
    ):
        result = score_task_history(
            _observations(route_id, family, items), route_id=route_id, task_family=family, as_of=now
        )
        n_all = result.observed_n
        if result.observed_n >= min_sample:
            return SuccessRank(
                result.score,
                result.observed_n,
                key,
                node_kind,
                "session_ledger",
                window_days,
                min_sample,
            )
    return SuccessRank(
        NEUTRAL_SCORE, n_all, None, node_kind, "below_min_sample", window_days, min_sample
    )
