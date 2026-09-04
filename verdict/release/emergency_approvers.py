"""Named emergency-approver registry for gate_unavailable-scope waivers.

FR-011 requires a full gate-infrastructure outage waiver to be attributed to
a named emergency approver, never an arbitrary reviewer. The registry is a
checked-in, version-controlled list (not a CI secret or runtime env var) so
that adding or removing an approver goes through code review, matching
Constitution V (Safety, Reversibility, and Least Authority).
"""

from __future__ import annotations

# Attributed by GitHub handle (matches the `reviewer` field recorded on a
# Waiver). Empty by default: no one is an emergency approver until named
# here explicitly, via a reviewed PR.
EMERGENCY_APPROVERS: frozenset[str] = frozenset(
    {
        # "github-handle",
    }
)


def is_emergency_approver(reviewer: str) -> bool:
    """Return True if `reviewer` is a registered emergency approver."""
    return reviewer in EMERGENCY_APPROVERS
