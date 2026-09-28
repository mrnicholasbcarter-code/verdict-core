"""Global concurrency governor for multi-story orchestration.

Pure library — deterministic, no I/O, no network calls.  All inputs are
passed as typed values.  Unknown or missing evidence fails closed
(SERIALIZE / DEFER), never defaults to parallel.

BOD-157 / BOD-203: the governor controls *admission* into the active set.
OpenJev only advises inside the admitted set; it never admits, revives
drops, invents identities, or overrides live health/quota/cooldown.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    """Outcome of an admission request."""

    ADMIT = "ADMIT"
    DEFER = "DEFER"
    SERIALIZE = "SERIALIZE"


@dataclass(frozen=True)
class AdmitRequest:
    """What the caller wants to run.

    Attributes:
        story_id: Unique story identifier.
        needs_coding_worker: Whether a coding worker slot is required.
        needs_integration_slot: Whether an integration slot is required.
        provider_pool: Provider pool name the work needs (empty → any).
    """

    story_id: str
    needs_coding_worker: bool = False
    needs_integration_slot: bool = False
    provider_pool: str = ""


@dataclass(frozen=True)
class ResourcePressure:
    """System resource pressure, each value in [0.0, 1.0].

    Values outside the range are clamped.  Missing evidence (NaN) is
    treated as *high pressure* (fail-closed).
    """

    cpu: float = 0.0
    ram: float = 0.0
    disk: float = 0.0

    def max_pressure(self) -> float:
        """Return the highest pressure across all dimensions, NaN → 1.0."""
        values = [self.cpu, self.ram, self.disk]
        return max(_clamp(v) for v in values)


@dataclass(frozen=True)
class RunState:
    """Snapshot of current running counts and pool health.

    Attributes:
        running_stories: Number of stories currently running.
        running_coding_workers: Number of coding worker slots in use.
        running_integration_slots: Number of integration slots in use.
        story_is_already_running: Whether the *requesting* story already
            has at least one running worker (prevents starvation).
        pool_healthy_capacity: Mapping of provider-pool name to the
            count of healthy eligible candidates.  A pool absent from
            the mapping is treated as unknown → fail-closed (DEFER).
    """

    running_stories: int = 0
    running_coding_workers: int = 0
    running_integration_slots: int = 0
    story_is_already_running: bool = False
    pool_healthy_capacity: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class GovernorResult:
    """Admission decision with a human-readable reason."""

    decision: Decision
    reason: str


@dataclass(frozen=True)
class GovernorCaps:
    """Configurable concurrency caps.

    Each cap must be >= 1.  The constructor clamps values below 1 up to 1.
    """

    max_stories: int = 3
    max_coding_workers: int = 6
    max_integration_slots: int = 2

    def __post_init__(self) -> None:
        # frozen=True means we use object.__setattr__ for clamping.
        object.__setattr__(self, "max_stories", max(1, self.max_stories))
        object.__setattr__(self, "max_coding_workers", max(1, self.max_coding_workers))
        object.__setattr__(self, "max_integration_slots", max(1, self.max_integration_slots))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(v: float) -> float:
    """Clamp *v* to [0.0, 1.0]; NaN → 1.0 (fail-closed)."""
    if math.isnan(v):
        return 1.0
    return max(0.0, min(1.0, v))


def _effective_cap(base: int, pressure: float) -> int:
    """Lower *base* cap linearly with pressure, floor 1.

    At pressure 0.0 the full cap is available.  At pressure 1.0 only 1
    slot is available.  Intermediate values interpolate linearly and
    round down.
    """
    # effective = base * (1 - pressure), clamped to [1, base]
    raw = base * (1.0 - pressure)
    return max(1, min(base, int(raw)))


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------


class GlobalConcurrencyGovernor:
    """Stateless concurrency gate.

    Call :meth:`admit` with the current snapshot; the governor never
    mutates external state.
    """

    def __init__(self, caps: GovernorCaps | None = None) -> None:
        self._caps = caps or GovernorCaps()

    @property
    def caps(self) -> GovernorCaps:
        return self._caps

    def admit(
        self, request: AdmitRequest, state: RunState, pressure: ResourcePressure | None = None
    ) -> GovernorResult:
        """Decide whether *request* should be admitted.

        Returns ADMIT, DEFER, or SERIALIZE with a reason string.

        Decision order (first match wins):
        1. Provider pool exhausted → DEFER.
        2. Story already running → ADMIT (no starvation of in-flight work).
        3. Resource pressure reduces effective caps.
        4. Story cap exceeded → SERIALIZE (wait for a story slot).
        5. Coding-worker cap exceeded → DEFER.
        6. Integration-slot cap exceeded → DEFER.
        7. Otherwise → ADMIT.
        """
        p = pressure or ResourcePressure()
        max_p = p.max_pressure()

        # 1. Pool health — fail-closed on unknown pool.
        if request.provider_pool:
            healthy = state.pool_healthy_capacity.get(request.provider_pool)
            if healthy is None:
                return GovernorResult(
                    Decision.DEFER,
                    f"provider pool {request.provider_pool!r} health unknown; failing closed",
                )
            if healthy <= 0:
                return GovernorResult(
                    Decision.DEFER,
                    f"provider pool {request.provider_pool!r} has 0 healthy candidates",
                )

        # 2. In-flight story protection (no starvation).
        if state.story_is_already_running:
            return GovernorResult(
                Decision.ADMIT,
                f"story {request.story_id!r} already running; admitted to prevent starvation",
            )

        # 3. Compute effective caps under pressure.
        eff_stories = _effective_cap(self._caps.max_stories, max_p)
        eff_coding = _effective_cap(self._caps.max_coding_workers, max_p)
        eff_integration = _effective_cap(self._caps.max_integration_slots, max_p)

        # 4. Story cap.
        if state.running_stories >= eff_stories:
            return GovernorResult(
                Decision.SERIALIZE,
                f"story cap reached ({state.running_stories}/{eff_stories}"
                f"{'' if max_p == 0.0 else f', pressure={max_p:.2f}'}); serialize",
            )

        # 5. Coding-worker cap.
        if request.needs_coding_worker and state.running_coding_workers >= eff_coding:
            return GovernorResult(
                Decision.DEFER,
                f"coding-worker cap reached ({state.running_coding_workers}/{eff_coding}"
                f"{'' if max_p == 0.0 else f', pressure={max_p:.2f}'}); deferring",
            )

        # 6. Integration-slot cap.
        if request.needs_integration_slot and state.running_integration_slots >= eff_integration:
            return GovernorResult(
                Decision.DEFER,
                f"integration-slot cap reached ({state.running_integration_slots}/{eff_integration}"
                f"{'' if max_p == 0.0 else f', pressure={max_p:.2f}'}); deferring",
            )

        # 7. All clear.
        return GovernorResult(Decision.ADMIT, "within all caps; admitted")
