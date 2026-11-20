# Design: Tier-0 escalation restricted to subscription/free capacity

## Context

**KNOWN** (confirmed from code at HEAD): The existing escalation path
(`_record_failure`, `_drive` in `verdict/orchestration/runtime.py`) sets
`escalation_tier_ceiling = last_selected_tier - 1` when `judged_failure_count
>= 2` and `last_selected_tier > 1`.  When `last_selected_tier == 1` the `else`
branch emits `escalation_exhausted` immediately without attempting tier-0.
`CapacityClass` has four values: `FREE (0)`, `SUBSCRIPTION (1)`, `METERED (2)`,
`UNKNOWN (3)`. `classify_route_capacity` derives these from account evidence
(oauth → SUBSCRIPTION, apikey+price → METERED, import_free_only → FREE, absent
evidence → UNKNOWN); model names never decide.

**INFERRED** (this change): a new `escalation_capacity_classes` field on
`TaskRequirements` restricts tier-0 escalation selection to the declared
capacity classes.  Eligibility applies this filter lazily (at select time),
so no pre-scan of the route pool is required.

## Goals / Non-Goals

**Goal**: tier-1 routes that exhaust K=2 judged failures attempt tier-0
subscription/free routes before emitting `escalation_exhausted`.

**Goal**: METERED and UNKNOWN-capacity tier-0 routes are NEVER the target of
an escalation path selection.

**Goal**: non-escalated (first-choice) selection is completely unchanged —
`escalation_capacity_classes` defaults to `frozenset()` which means no
restriction.

**Non-goal**: a general "spend guard" preventing tier-0 from normal
(non-escalated) selection.  Normal selection ranking already prefers the
cheapest sufficient route, which for subscription pools is tier-1 or tier-2.

## Decisions

### 1. Lazy check: restricted selection returning None IS the check

No pre-scan of the route pool before setting the ceiling.  The runtime sets
`escalation_tier_ceiling=0` and `escalation_capacity_classes` in
`_record_failure`, then `_drive` runs the restricted selection.  If no route
qualifies, `_esc_choice is None` → emit `escalation_exhausted` from `_drive`.
This avoids adding a sync query to the hot path in `_record_failure`.

### 2. Capacity classes on TaskRequirements, not EligibilityLadder

The filter lives on `TaskRequirements.escalation_capacity_classes` so it is
explicit in the call site, composable, and testable without constructing a full
ladder.  The eligibility filter fires at `TASK_ELIGIBLE` stage (after tier
check) so it is counted/visible in rejection stats.

### 3. escalation_exhausted moves from _record_failure to _drive for t==1

For t==0 (already at frontier): `escalation_exhausted` still fires from
`_record_failure` immediately.  For t==1 (new): `_record_failure` sets the
ceiling; `_drive` emits `escalation_exhausted` only when no qualifying tier-0
route is found.  This preserves the existing event for t==0 while making t==1
retry attempt tier-0 first.

## Security / trust implications

No new trust boundary is crossed.  The capacity class is derived from account
evidence (never from model names or external untrusted data).  The filter only
narrows the selection; it cannot widen admission or bypass existing gates.
METERED routes are excluded from escalation but remain available through normal
(non-escalated) selection — no routes are blacklisted.

## Routing / context / memory implications

`escalation_capacity_classes` is a pure in-memory field on `TaskRequirements`
and `NodeRun`.  It is not persisted to any state file and is not stored in
memory/context stores.  It has no effect on the admission proof, the receipt
digest, or the events schema beyond the new `capacity_classes_allowed` field in
the `escalation` event and the new field in `escalation_exhausted` events.
No OmniRoute context pack changes are required.

## Risks / Trade-offs

**Risk**: tier-0 subscription route exists but probe evidence is stale →
selected but fails.  Mitigation: same as all escalation paths — the attempt
budget bounds retries and the tier-0 route goes into tried after failure.

**Risk**: `escalation_tier0_capacity_classes` default changes behaviour for
operators who have only METERED tier-0 routes.  Mitigation: they were already
getting `escalation_exhausted` on t==1; the new code also gives exhausted (no
regression).  METERED tier-0 is reachable via normal fallback selection.

**Trade-off**: `escalation_exhausted` from `_drive` fires every loop iteration
where ceiling==0 and no route is found, not just once.  If the runtime keeps
looping with the ceiling set, multiple exhausted events accumulate.  This is
consistent with the existing behaviour for t==0 (exhausted emitted from
`_record_failure` on each qualifying failure).  Tests accept `>= 1` occurrences.
