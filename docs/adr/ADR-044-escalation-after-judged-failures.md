# ADR-044: Escalation after judged failures

**Status:** Accepted (merged in #1182 and #1189)
**Date:** 2026-11-20
**Deciders:** Operator
**Related:** ADR-0001 (control-plane invariants), ADR-036 (goal-to-receipt
orchestration), ADR-042 (dynamic role-based model selection)

## Context

Real-story runs in production showed orchestration nodes exhausting their
attempt budget on the same route without trying a stronger model. A tier-1
Haiku-class route with `slack=0` always ranked first under the pre-#1194
ordering (rating before slack), so every attempt on a node landed on that
route. If the route produced wrong output, the node blocked rather than
escalating (run 20261120T013732Z, real-story-6). Run 20261119T225440Z
(real-story-5) landed on a Sonnet-class route only because the Haiku-class
route happened to be cooled from prior runs.

Capability escalation is a bounded, monotone safety valve: after repeated
judged failures the runtime raises the capability floor so a stronger route
must be tried. It does not change the admission set for un-escalated
attempts.

## Decision

### What counts as a judged failure

A failure on a node counts toward the escalation counter when either:

- the reviewer validated the work and found it wrong (`validated=True`), or
- the failure category is in `_ESCALATION_JUDGED_CATEGORIES`, which is
  `{"diff_rejected", "verification_failed"}`.

Interrupted failures (transport errors, timeouts) do not count.

### Threshold and ceiling

After `_ESCALATION_JUDGED_FAILURE_THRESHOLD = 2` judged failures on a node,
the runtime sets `NodeRun.escalation_tier_ceiling` and applies it on the next
attempt. The ceiling is monotone: it can only tighten, never widen.

Escalation behaviour by tier of the last selected route (`last_selected_tier`):

| Last tier | Action |
|---|---|
| `> 1` (e.g. tier 2 or 3) | `escalation_tier_ceiling = last_selected_tier − 1` |
| `None` (unknown tier) | `escalation_tier_ceiling = _ESCALATION_UNKNOWN_TIER_CEILING` (3), which keeps all routes with a measured capability tier while excluding unknown-tier routes |
| `1` (Sonnet-class) | `escalation_tier_ceiling = 0`; capacity classes restricted to `RuntimePolicy.escalation_tier0_capacity_classes` (default `{SUBSCRIPTION, FREE}`) |
| `0` (already frontier) | `escalation_exhausted` emitted from `_drive`; runtime falls back to the full admitted set |

### TaskRequirements fields

`escalation_max_tier: int | None = None` — `None` means no ceiling is active
(ordinary selection). When set, `EligibilityLadder._assess` excludes routes
whose capability tier exceeds the ceiling with reason
`"below_escalation_floor"`.

`escalation_capacity_classes: frozenset[CapacityClass] | None = None` — `None`
means no capacity restriction (non-escalated selection). When set to a
non-empty frozenset, only routes whose capacity class is in the set are
eligible; others are excluded with reason `"escalation_capacity_excluded"`.
When set to an empty frozenset (operator policy `--capacity metered` with no
subscription/free routes), all routes are excluded and `_drive` emits
`escalation_exhausted`.

### No pay-per-token route is ever opened by escalation

The default `RuntimePolicy.escalation_tier0_capacity_classes = {SUBSCRIPTION,
FREE}` means METERED-capacity and UNKNOWN-capacity tier-0 routes are never
selected through the escalation path. The `--capacity free` operator filter
sets this to `{FREE}` only, so only free-tier tier-0 routes qualify.

### Fallback on exhaustion

When the escalation path produces no candidates (capacity-restricted or no
stronger route exists), `_drive` emits `"escalation_exhausted"` and continues
with the full admitted set so the attempt budget remains bounded. No paid route
is ever opened solely because escalation exhausted.

### Admission invariants preserved

- `exclude_routes` (the set of previously-tried routes) passes through both the
  escalation and fallback branches.
- Opaque routes (`auto/*`, `combo/*`) are never candidates and are not affected
  by escalation.
- The escalation ceiling is a narrowing filter. Admission membership can only
  narrow, never expand.

## Consequences

- After two wrong outputs the runtime automatically tries a stronger model for
  the same node, reducing manual intervention for capability-related failures.
- The ceiling is monotone, so an accidental failure on a strong route does not
  cause an unbounded search upward.
- The two-PR split (PR #1182: base escalation for tier > 1 and unknown-tier
  routes; PR #1189: tier-1 → tier-0 escalation restricted to subscription/free)
  is recorded in the OpenSpec changes
  `capability-escalation-after-failures` and `escalate-to-subscription-tier0`.
  Merge SHAs: `7086f15827e41f026637c659ba873865a6d43ff8` (#1182),
  `5ac29211a8e5d1dc30a402671e6342b1bfa69918` (#1189).
