"""
Pure advisory reordering for OpenJev ADVISORY mode (BOD-238).

advise_order() accepts an admitted candidate list and a DecisionSignalSetV1
(or None) and returns the same candidates in advisory order plus an
InfluenceRecord.  It never adds, removes, or restores candidates.

AdvisoryRanker wraps advise_order() in an AdaptiveRanker-compatible interface
so it can be passed as AdvisoryInput.ranker to decision_kernel.decide().

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

import hashlib
import json
import logging
import os
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from verdict.decision_signals.contracts import DecisionSignalSetV1
    from verdict.eligibility import EligibilityResult

logger = logging.getLogger(__name__)

# Public cut-off names (documented as uncalibrated initial values — BOD-203).
_DEFAULT_MIN_CONFIDENCE = 0.6
_FRONTIER_WORTHY_KEY = "frontier_worthy"
_COMPLEXITY_KEY = "complexity"
_ECONOMY_THRESHOLD = 0.4  # both must be < this for "economy"
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


def _signals_digest(signals: DecisionSignalSetV1 | None) -> str | None:
    if signals is None:
        return None
    return getattr(signals, "input_digest", None)


def advise_order(
    candidates: list[Any],
    signals: DecisionSignalSetV1 | None,
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
    cut_confidence = (
        min_confidence
        if min_confidence is not None
        else float(os.environ.get("VERDICT_ADVISORY_MIN_CONFIDENCE", str(_DEFAULT_MIN_CONFIDENCE)))
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
        # prefer cheapest: lowest inferred cost first, then highest tier number
        # (tier-3 is cheaper/weaker than tier-1) as tiebreak, then stable id.
        # Cost is derived from ModelInfo.pricing (keys "input" and "output", cost
        # per 1k tokens) when present; falls back to cost_per_1k, then tier proxy.
        # Both pricing and tier-proxy are documented as uncalibrated (BOD-203).
        def _economy_cost(m: Any) -> tuple[float, int, str, str]:
            p: dict[str, float] = getattr(m, "pricing", None) or {}
            if p:
                cost = float(p.get("input", 0.0)) + float(p.get("output", 0.0))
            else:
                cpk = getattr(m, "cost_per_1k", None)
                if cpk is not None:
                    cost = float(cpk)
                else:
                    # Tier proxy: tier 1=0.0, tier 2=0.5, tier 3=1.0 (inverted —
                    # higher tier is cheaper, so we want smaller sort key = higher tier).
                    cost = max(0.0, 1.0 - float(getattr(m, "capability_tier", 2)) / 4.0)
            # Secondary tiebreak: highest tier number first (= cheaper model class).
            tier_inv = -(getattr(m, "capability_tier", 0))
            return (cost, tier_inv, getattr(m, "provider", ""), getattr(m, "id", ""))

        ordered = sorted(candidates, key=_economy_cost)
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


class AdvisoryRanker:
    """AdaptiveRanker-compatible wrapper around advise_order().

    Used as AdvisoryInput.ranker in decision_kernel.decide().
    decide() enforces membership invariance (drops non-admitted ids, re-appends
    omitted admitted ones), so we only need to return the right order.

    Signals are supplied at construction time (typically already collected by
    the caller before the kernel call).
    """

    def __init__(
        self,
        signals: DecisionSignalSetV1 | None,
        *,
        protected: bool = False,
        privacy: str | None = None,
        min_confidence: float | None = None,
    ) -> None:
        self._signals = signals
        self._protected = protected
        self._privacy = privacy
        self._min_confidence = min_confidence
        # Expose a minimal config attribute that decision_kernel reads.
        from verdict.adaptive_ranker import AdaptiveRankerConfig, CanaryPolicy, RankerMode

        self.config = AdaptiveRankerConfig(
            mode=RankerMode.SHADOW_ADAPTIVE, canary_policy=CanaryPolicy.DISABLED
        )

    def rank(self, eligibility_result: EligibilityResult, task_spec: Any) -> Any:
        """Return a RankerOutput with advisory-reordered admitted candidates."""
        from verdict.adaptive_ranker import CanaryPolicy, RankerMode, RankerOutput

        admitted = list(eligibility_result.admitted)
        ordered, influence = advise_order(
            admitted,
            self._signals,
            protected=self._protected,
            privacy=self._privacy,
            min_confidence=self._min_confidence,
        )
        scores = {getattr(m, "id", ""): 1.0 - i * 0.01 for i, m in enumerate(ordered)}
        reasoning = {getattr(m, "id", ""): f"advisory_{influence.profile}" for m in ordered}
        # Compute a stable candidate-set hash for the receipt.
        ids = sorted(getattr(m, "id", "") for m in admitted)
        csh = hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16]

        return RankerOutput(
            ranked=ordered,
            scores=scores,
            reasoning=reasoning,
            candidate_set_hash=csh,
            eligibility_hash=csh,
            mode=RankerMode.SHADOW_ADAPTIVE,
            shadow=True,
            version="advisory-ranker/v1",
            canary_policy=CanaryPolicy.DISABLED,
        )


__all__ = ["AdvisoryRanker", "InfluenceRecord", "_mode_from_env", "advise_order"]
