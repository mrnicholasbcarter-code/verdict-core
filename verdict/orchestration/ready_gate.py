"""Ready-gate: per-story READY / EXCLUDED / WAIT decision (BOD-157 AC4/AC5).

Pure library — deterministic, no network, no Linear calls.
All inputs are typed values; unknown or missing evidence fails closed (WAIT).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class ReadyVerdict(str, Enum):
    """Outcome of a ready-gate evaluation."""

    READY = "READY"
    EXCLUDED = "EXCLUDED"
    WAIT = "WAIT"


@dataclass(frozen=True)
class DepEvidence:
    """Evidence about a single dependency story.

    Parameters
    ----------
    identifier:
        Opaque dependency identifier (e.g. issue key).
    linear_done:
        Whether Linear reports this dependency as Done/Completed.
        ``None`` means unknown.
    merge_commit_on_main:
        Whether the dependency's merge commit is an ancestor of *main_sha*.
        ``None`` means unknown / not checked.
    verification_record_present:
        Whether a verification record exists for this dependency.
        ``None`` means unknown / not checked.
    """

    identifier: str
    linear_done: bool | None = None
    merge_commit_on_main: bool | None = None
    verification_record_present: bool | None = None

    @property
    def main_verified(self) -> bool:
        """True only when BOTH merge-on-main AND verification record are confirmed."""
        return self.merge_commit_on_main is True and self.verification_record_present is True


@dataclass(frozen=True)
class ReadyDecision:
    """Result of :func:`ready_decision`."""

    verdict: ReadyVerdict
    reason: str


def ready_decision(
    *, labels: frozenset[str], deps: Sequence[DepEvidence], main_sha: str
) -> ReadyDecision:
    """Decide whether a story is READY for concurrent dispatch.

    Parameters
    ----------
    labels:
        Set of labels on the story (e.g. from Linear).
    deps:
        Evidence records for each dependency.
    main_sha:
        Current main branch HEAD SHA (used only as documentation; ancestry
        checks are pre-computed in *deps*).

    Returns
    -------
    ReadyDecision
        ``EXCLUDED`` if the story carries ``automation:manual``.
        ``WAIT`` if any dependency is not MAIN_VERIFIED or has unknown evidence.
        ``READY`` otherwise.

    The function is deterministic and performs no I/O.
    """
    # --- Rule 1: manual label → EXCLUDED ----------------------------------
    if "automation:manual" in labels:
        return ReadyDecision(
            verdict=ReadyVerdict.EXCLUDED,
            reason="label 'automation:manual' present; story excluded from auto-dispatch",
        )

    # --- Rule 2: no deps → READY ------------------------------------------
    if not deps:
        return ReadyDecision(verdict=ReadyVerdict.READY, reason="no dependencies; story is ready")

    # --- Rule 3: check each dependency ------------------------------------
    waiting: list[str] = []
    for dep in deps:
        if dep.main_verified:
            continue  # this dependency is satisfied

        # Distinguish "done in Linear but not verified on main" from unknown.
        if dep.linear_done is True:
            waiting.append(f"dep '{dep.identifier}' is Done in Linear but not MAIN_VERIFIED")
        elif dep.merge_commit_on_main is None or dep.verification_record_present is None:
            waiting.append(
                f"dep '{dep.identifier}' has unknown/missing evidence; fail-closed to WAIT"
            )
        else:
            waiting.append(f"dep '{dep.identifier}' is not MAIN_VERIFIED")

    if waiting:
        return ReadyDecision(verdict=ReadyVerdict.WAIT, reason="; ".join(waiting))

    return ReadyDecision(
        verdict=ReadyVerdict.READY, reason="all dependencies are MAIN_VERIFIED; story is ready"
    )
