"""
Pure advisory reordering for OpenJev ADVISORY mode (BOD-238).

advise_order() accepts an admitted candidate list and a DecisionSignalSetV1
(or None) and returns the same candidates in advisory order plus an
InfluenceRecord.  It never adds, removes, or restores candidates.

Mode is read from VERDICT_DECISION_SIGNALS_MODE:
  OFF (default) | SHADOW | ADVISORY
Invalid values are treated as OFF (warning emitted).

Profiles
--------
economy  : frontier_worthy < 0.4 AND complexity < 0.4
             -> cheapest (lowest tier, then cheapest provider, then id)
strength : frontier_worthy >= 0.6 OR complexity >= 0.6
             -> strongest (highest tier, then quality_confidence, then id)
otherwise: no change (profile="inconclusive")

Hard skip conditions (returns baseline unchanged)
-------------------------------------------------
* task is protected (critical tier / protected_policy)
* privacy is "restricted" or "trusted_upstream"
* signals is None or failure_class is set
* confidence < VERDICT_ADVISORY_MIN_CONFIDENCE (default 0.6)
* mode is not ADVISORY
* provider is None / missing
* any exception or timeout in the provider call (caller enforces timeout)
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from verdict.decision_signals.contracts import DecisionSignalSetV1

logger = logging.getLogger(__name__)

# Public cut-off names (documented as uncalibrated initial values — BOD-203).
_DEFAULT_MIN_CONFIDENCE = 0.6
_FRONTIER_WORTHY_KEY = "frontier_worthy"
_COMPLEXITY_KEY = "complexity"
_ECONOMY_THRESHOLD = 0.4   # both must be < this for "economy"
_STRENGTH_THRESHOLD = 0.6  # either must be >= this for "strength"


def _mode_from_env() -> str:
    """Return OFF | SHADOW | ADVISORY; invalid -> OFF with warning."""
    raw = os.environ.get("VERDICT_DECISION_SIGNALS_MODE", "OFF").strip().upper()
    if raw not in ("OFF", "SHADOW", "ADVISORY"):
        warnings.warn(
            f"Invalid VERDICT_DECISION_SIGNALS_MODE={raw!r}, treating as OFF. "
            "Allowed: OFF, SHADOW, ADVISORY",
            UserWarning,
            stacklevel=3,
        )
        return "OFF"
    return raw


@dataclass(frozen=True)
class InfluenceRecord:
    """Evidence of advisory reordering for one route call."""

    applied: bool
    """True when a reorder was actually applied (profile economy or strength)."""
    profile: str
    """economy | strength | inconclusive | skipped:<reason>"""
    reason: str
    """Human-readable reason code."""
    baseline_first: str | None
    """ID of the first candidate before advisory, or None when empty."""
    advised_first: str | None
    """ID of the first candidate after advisory, or None when empty / skipped."""
    signals_digest: str | None
    """input_digest from the signal set, or None when no signals used."""


def _signals_digest(signals: "DecisionSignalSetV1 | None") -> str | None:
    if signals is None:
        return None
    return getattr(signals, "input_digest", None)


def advise_order(
    candidates: list[Any],
    signals: "DecisionSignalSetV1 | None",
    *,
    protected: bool = False,
    privacy: str | None = None,
    min_confidence: float | None = None,
) -> tuple[list[Any], InfluenceRecord]:
    """Return candidates in advisory order plus an InfluenceRecord.

    Parameters
    ----------
    candidates:
        The admitted candidate list (ModelInfo objects).  Membership is never
        changed; only order may differ.
    signals:
        A DecisionSignalSetV1 collected from the provider, or None.
    protected:
        True when the task is critical-tier or protected_policy.
    privacy:
        Task privacy value.  Skips advisory for "restricted" or
        "trusted_upstream".
    min_confidence:
        Override for VERDICT_ADVISORY_MIN_CONFIDENCE (default 0.6).

    Returns
    -------
    (ordered_candidates, influence_record)
        ordered_candidates has the same members as candidates, possibly
        reordered.  influence_record carries the applied/profile/reason.
    """
    baseline_first = getattr(candidates[0], "id", None) if candidates else None
    cut_confidence = min_confidence if min_confidence is not None else float(
        os.environ.get("VERDICT_ADVISORY_MIN_CONFIDENCE", str(_DEFAULT_MIN_CONFIDENCE))
    )

    def _skip(reason: str, profile: str = "skipped") -> tuple[list[Any], InfluenceRecord]:
        return list(candidates), InfluenceRecord(
            applied=False,
            profile=f"skipped:{reason}",
            reason=reason,
            baseline_first=baseline_first,
            advised_first=baseline_first,
            signals_digest=_signals_digest(signals),
        )

    # --- hard skip guards ------------------------------------------------
    if protected:
        return _skip("protected")
    if privacy in ("restricted", "trusted_upstream"):
        return _skip("privacy_restricted")
    if signals is None:
        return _skip("no_signals")
    if getattr(signals, "failure_class", None) is not None:
        return _skip("failure_class_set")
    sig_confidence = getattr(signals, "confidence", 0.0)
    if sig_confidence < cut_confidence:
        return _skip("low_confidence")

    raw_signals: dict[str, float] = getattr(signals, "signals", None) or {}
    if not raw_signals:
        return _skip("empty_signals")

    fw = raw_signals.get(_FRONTIER_WORTHY_KEY, 0.5)
    cx = raw_signals.get(_COMPLEXITY_KEY, 0.5)

    # --- profile determination -------------------------------------------
    if fw < _ECONOMY_THRESHOLD and cx < _ECONOMY_THRESHOLD:
        profile = "economy"
    elif fw >= _STRENGTH_THRESHOLD or cx >= _STRENGTH_THRESHOLD:
        profile = "strength"
    else:
        # inconclusive: no change
        return list(candidates), InfluenceRecord(
            applied=False,
            profile="inconclusive",
            reason="signals_inconclusive",
            baseline_first=baseline_first,
            advised_first=baseline_first,
            signals_digest=_signals_digest(signals),
        )

    if not candidates:
        return [], InfluenceRecord(
            applied=False,
            profile=profile,
            reason="empty_candidate_list",
            baseline_first=None,
            advised_first=None,
            signals_digest=_signals_digest(signals),
        )

    # --- reorder ---------------------------------------------------------
    if profile == "economy":
        # prefer cheapest: lowest capability_tier, then provider priority
        # (alphabetical as a proxy — provider priority values are caller-owned),
        # then stable id.
        ordered = sorted(
            candidates,
            key=lambda m: (
                getattr(m, "capability_tier", 99),
                getattr(m, "provider", ""),
                getattr(m, "id", ""),
            ),
        )
    else:  # strength
        # prefer strongest: highest quality_confidence, then highest tier
        # (inverted), then stable id.
        ordered = sorted(
            candidates,
            key=lambda m: (
                -(getattr(m, "quality_confidence", None) or 0.0),
                getattr(m, "capability_tier", 99),
                getattr(m, "id", ""),
            ),
        )

    advised_first = getattr(ordered[0], "id", None) if ordered else None
    applied = advised_first != baseline_first or ordered != candidates

    return ordered, InfluenceRecord(
        applied=applied,
        profile=profile,
        reason=f"advisory_{profile}",
        baseline_first=baseline_first,
        advised_first=advised_first,
        signals_digest=_signals_digest(signals),
    )


__all__ = [
    "InfluenceRecord",
    "advise_order",
    "_mode_from_env",
]
