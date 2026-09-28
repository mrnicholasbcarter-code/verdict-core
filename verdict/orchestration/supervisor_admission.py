"""Supervisor admission pipeline — feature-flagged multi-story governor (BOD-157).

When VERDICT_MULTI_STORY=on, the admission pipeline runs:
  1. ready_decision() — READY / EXCLUDED / WAIT
  2. collide() against every running story — PARALLEL / SERIALIZE
  3. governor.admit() — ADMIT / DEFER / SERIALIZE

When the flag is off (default) or any unrecognised value, this module is inert:
the supervisor behaves identically to the single-story flock path on origin/main.

All defaults are *provisional pending operator decision* (ADR-037 §Open decisions).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from verdict.orchestration.concurrency_governor import (
    AdmitRequest,
    Decision,
    GlobalConcurrencyGovernor,
    GovernorCaps,
    GovernorResult,
    ResourcePressure,
    RunState,
)
from verdict.orchestration.ready_gate import (
    DepEvidence,
    ReadyDecision,
    ReadyVerdict,
    ready_decision,
)
from verdict.orchestration.story_footprint import CollisionVerdict, StoryFootprintV1, collide

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provisional defaults (ADR-037 §Open decisions — most conservative)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorGovernorConfig:
    """Provisional defaults.  Every value here is pending operator decision.

    Attributes
    ----------
    max_stories : int
        Maximum concurrent stories.  Conservative: 2.
    max_coding_workers : int
        Maximum coding-worker slots across all stories.  Conservative: 2.
    max_integration_slots : int
        Integration (merge/rebase) is always serialised.  Fixed: 1.
    """

    max_stories: int = 2
    max_coding_workers: int = 2
    max_integration_slots: int = 1


DEFAULT_CONFIG = SupervisorGovernorConfig()


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_MULTI_STORY_ENV = "VERDICT_MULTI_STORY"


def multi_story_enabled() -> bool:
    """Return True only when the flag is explicitly ``"on"`` (case-insensitive).

    Any other value (including absent, empty, ``"off"``, ``"yes"``, ``"1"``)
    returns False and logs a warning for unrecognised non-empty values.
    """
    raw = os.environ.get(_MULTI_STORY_ENV, "").strip().lower()
    if raw == "on":
        return True
    if raw and raw != "off":
        log.warning(
            "VERDICT_MULTI_STORY=%r is not a recognised value (expected 'on' or 'off'); "
            "treating as OFF",
            os.environ.get(_MULTI_STORY_ENV, ""),
        )
    return False


# ---------------------------------------------------------------------------
# Running-story registry (used only when flag is on)
# ---------------------------------------------------------------------------


@dataclass
class RunningStory:
    """Bookkeeping for a concurrently running story."""

    story_id: str
    footprint: StoryFootprintV1


@dataclass
class AdmissionState:
    """Mutable state tracking running stories for the governor pipeline.

    Thread-safe: all mutations hold *_lock*.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _running: dict[str, RunningStory] = field(default_factory=dict)
    _integration_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- queries -----------------------------------------------------------

    @property
    def running_stories(self) -> list[RunningStory]:
        with self._lock:
            return list(self._running.values())

    @property
    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    # -- mutations ---------------------------------------------------------

    def add(self, story: RunningStory) -> None:
        with self._lock:
            self._running[story.story_id] = story

    def remove(self, story_id: str) -> None:
        with self._lock:
            self._running.pop(story_id, None)

    # -- integration serialisation -----------------------------------------

    @property
    def integration_lock(self) -> threading.Lock:
        """Callers acquire this to serialise merge/rebase to main."""
        return self._integration_lock


# ---------------------------------------------------------------------------
# Admission decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionDecision:
    """Result of :func:`evaluate_admission`."""

    admit: bool
    reason_code: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admit": self.admit,
            "reason_code": self.reason_code,
            "reason": self.reason,
            **self.detail,
        }


def evaluate_admission(
    *,
    story_id: str,
    labels: frozenset[str],
    deps: list[DepEvidence],
    main_sha: str,
    footprint: StoryFootprintV1,
    admission_state: AdmissionState,
    config: SupervisorGovernorConfig | None = None,
    pressure: ResourcePressure | None = None,
) -> AdmissionDecision:
    """Run the full ADR-037 admission pipeline for *story_id*.

    Returns an :class:`AdmissionDecision` with *admit=True* only when all
    three gates pass.  Fail-closed on any unknown/missing evidence.
    """
    cfg = config or DEFAULT_CONFIG
    pressure = pressure or ResourcePressure()  # unknown → 0.0 per-dim; governor NaN→1.0

    # --- Gate 1: ready_decision -------------------------------------------
    rd: ReadyDecision = ready_decision(labels=labels, deps=deps, main_sha=main_sha)
    if rd.verdict == ReadyVerdict.EXCLUDED:
        return AdmissionDecision(admit=False, reason_code="EXCLUDED", reason=rd.reason)
    if rd.verdict == ReadyVerdict.WAIT:
        return AdmissionDecision(admit=False, reason_code="WAIT", reason=rd.reason)

    # --- Gate 2: collision detection --------------------------------------
    running = admission_state.running_stories
    for rs in running:
        verdict, col_reason = collide(footprint, rs.footprint)
        if verdict == CollisionVerdict.SERIALIZE:
            return AdmissionDecision(
                admit=False,
                reason_code="SERIALIZE",
                reason=f"collision with running story {rs.story_id!r}: {col_reason.value}",
                detail={"collides_with": rs.story_id, "collision_reason": col_reason.value},
            )

    # --- Gate 3: governor -------------------------------------------------
    caps = GovernorCaps(
        max_stories=cfg.max_stories,
        max_coding_workers=cfg.max_coding_workers,
        max_integration_slots=cfg.max_integration_slots,
    )
    governor = GlobalConcurrencyGovernor(caps=caps)
    run_state = RunState(
        running_stories=len(running),
        running_coding_workers=len(running),  # conservative: 1 worker per story
        running_integration_slots=0,
        story_is_already_running=any(rs.story_id == story_id for rs in running),
    )
    request = AdmitRequest(
        story_id=story_id, needs_coding_worker=True, needs_integration_slot=False
    )
    result: GovernorResult = governor.admit(request, run_state, pressure)

    if result.decision == Decision.ADMIT:
        return AdmissionDecision(admit=True, reason_code="ADMIT", reason=result.reason)
    return AdmissionDecision(admit=False, reason_code=result.decision.value, reason=result.reason)
