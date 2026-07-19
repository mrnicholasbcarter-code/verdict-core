"""Single-source-of-truth eligibility gate for pre-ranking filtering.

This module implements the issue #57 / #72 invariant: candidate filtering
happens *before* any adaptive or cost ranking, and no ranker, Ruflo plan, or
RuVector result can reintroduce a candidate that the gate excluded.

The gate consults the already-merged :class:`AvailabilityCache` (issue #56),
which wraps the :class:`OmniRouteAvailabilityAdapter`.  It is deliberately
protocol-based: it takes a ``Callable[[str], AvailabilityReport]`` so the live
routing path and the explain endpoint share identical truth.

Fail-closed semantics (per ROUTING_POLICY + #57 AC): when a request is
*protected* and the live availability truth is absent (``unknown`` / ``error``
/ missing), the candidate is excluded rather than optimistically admitted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from llm_gate.availability import AvailabilityReport, AvailabilityState
from llm_gate.models import ModelInfo


class EligibilityVerdict(str, Enum):
    """Why a candidate was kept or excluded by the gate."""

    ELIGIBLE = "eligible"
    NOT_LIVE_ELIGIBLE = "not_live_eligible"
    RUNTIME_TRUTH_ABSENT = "runtime_truth_absent"
    NOT_REQUESTED_TIER = "not_requested_tier"


# States that admit a candidate into the pre-ranking eligible set.
_ADMITTED_STATES = frozenset(
    {
        AvailabilityState.ELIGIBLE,
        AvailabilityState.READY,
        AvailabilityState.DEGRADED,
    }
)


@dataclass(frozen=True)
class EligibilityRecord:
    """Per-candidate gate outcome, preserved for the explain endpoint (#73)."""

    model_id: str
    provider: str
    admitted: bool
    verdict: str
    state: str
    source: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "admitted": self.admitted,
            "verdict": self.verdict.value
            if isinstance(self.verdict, EligibilityVerdict)
            else self.verdict,
            "state": self.state,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass
class EligibilityResult:
    """Outcome of filtering a candidate set before ranking."""

    admitted: list[ModelInfo] = field(default_factory=list)
    records: list[EligibilityRecord] = field(default_factory=list)

    @property
    def exclusions(self) -> list[EligibilityRecord]:
        return [r for r in self.records if not r.admitted]

    @property
    def eligible(self) -> list[ModelInfo]:
        return self.admitted

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": [m.id for m in self.admitted],
            "records": [r.to_dict() for r in self.records],
            "exclusions": [r.to_dict() for r in self.exclusions],
        }


def _state_for(report: AvailabilityReport | None, model_id: str) -> tuple[str, str]:
    """Return (state, source) for a model from its cached report."""
    if report is None:
        return ("unknown", "cache")
    for candidate in report.candidates:
        if (
            candidate.model.id == model_id
            or candidate.model.id.split("/", 1)[-1] == model_id.split("/", 1)[-1]
        ):
            return (candidate.state.value, candidate.source)
    # Candidate absent from the report entirely -> treat as unknown (fail-closed
    # for protected work; the router may still admit unverified in dev mode).
    return ("unknown", report.source)


class EligibilityGate:
    """Filter candidates by live eligibility before any ranking.

    The gate is the single authority consulted by the router, dispatcher, and
    the explain endpoint, so no downstream ranker can reintroduce an excluded
    candidate (issue #57 invariant).
    """

    def __init__(
        self,
        availability_source: Callable[[str], AvailabilityReport] | None,
        *,
        protected_fail_closed: bool = True,
        allow_unverified_in_dev: bool = True,
        clock: Any = None,
    ) -> None:
        # ``availability_source`` is the cache's ``get`` callable; ``None`` when
        # no OmniRoute endpoint is configured (explain-only mode).
        self.availability_source = availability_source
        self.protected_fail_closed = protected_fail_closed
        self.allow_unverified_in_dev = allow_unverified_in_dev

    def evaluate(
        self,
        candidates: list[ModelInfo],
        *,
        protected: bool = False,
        dev_mode: bool = False,
        now: Any = None,
    ) -> EligibilityResult:
        """Filter ``candidates`` to the pre-ranking eligible set.

        Args:
            candidates: Discovered ``ModelInfo`` from the catalog.
            protected: If True and runtime truth is absent, exclude (fail-closed).
            dev_mode: If True and truth absent and not protected, admit unverified.
            now: Optional clock (unused directly; kept for call-site symmetry).
        """
        result = EligibilityResult()
        for model in candidates:
            record = self._judge(model, protected=protected, dev_mode=dev_mode)
            result.records.append(record)
            if record.admitted:
                result.admitted.append(model)
        return result

    def _judge(self, model: ModelInfo, *, protected: bool, dev_mode: bool) -> EligibilityRecord:
        model_id = model.id
        provider = model.provider or "unknown"
        if self.availability_source is None:
            # No live availability configured: admit in dev, fail-closed for
            # protected work (cannot verify, so protect by exclusion).
            if protected and self.protected_fail_closed:
                return EligibilityRecord(
                    model_id=model_id,
                    provider=provider,
                    admitted=False,
                    verdict=EligibilityVerdict.RUNTIME_TRUTH_ABSENT,
                    state="unknown",
                    source="cache",
                    reason="protected work: availability cache not configured",
                )
            return EligibilityRecord(
                model_id=model_id,
                provider=provider,
                admitted=True,
                verdict=EligibilityVerdict.ELIGIBLE,
                state=model.availability_state,
                source="catalog",
            )

        report = self.availability_source(model_id)
        state, source = _state_for(report, model_id)

        if state in _ADMITTED_STATES:
            return EligibilityRecord(
                model_id=model_id,
                provider=provider,
                admitted=True,
                verdict=EligibilityVerdict.ELIGIBLE,
                state=state,
                source=source,
            )

        # Excluded by live truth.
        if state in {"unknown", "error", "unavailable", "timeout", "malformed", "unauthorized"}:
            if protected and self.protected_fail_closed:
                return EligibilityRecord(
                    model_id=model_id,
                    provider=provider,
                    admitted=False,
                    verdict=EligibilityVerdict.RUNTIME_TRUTH_ABSENT,
                    state=state,
                    source=source,
                    reason=f"protected work: live availability {state}",
                )
            if dev_mode and self.allow_unverified_in_dev and not protected:
                # Degraded/unknown in dev: admit but flag so ranking can prefer
                # verified candidates.
                return EligibilityRecord(
                    model_id=model_id,
                    provider=provider,
                    admitted=True,
                    verdict=EligibilityVerdict.NOT_LIVE_ELIGIBLE,
                    state=state,
                    source=source,
                    reason=f"dev mode admits unverified candidate ({state})",
                )
            return EligibilityRecord(
                model_id=model_id,
                provider=provider,
                admitted=False,
                verdict=EligibilityVerdict.RUNTIME_TRUTH_ABSENT,
                state=state,
                source=source,
                reason=f"live availability {state}",
            )

        # Denied / quota_exhausted / rate_limited / locked_out / circuit_open /
        # policy_denied: never admit.
        return EligibilityRecord(
            model_id=model_id,
            provider=provider,
            admitted=False,
            verdict=EligibilityVerdict.NOT_LIVE_ELIGIBLE,
            state=state,
            source=source,
            reason=f"candidate excluded: {state}",
        )
