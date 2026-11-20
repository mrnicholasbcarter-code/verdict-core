# Tier-0 escalation restricted to subscription/free capacity

## Why

Evidence from real-story runs (BOD-342 D15) shows tier-1 (Sonnet-class) routes
failing repeatedly with no escalation path: when `last_selected_tier == 1` the
existing code emits `escalation_exhausted` immediately and never attempts tier-0
(frontier) routes, even though Claude Max / Opus subscription routes are
available at zero marginal cost.

The operator standing rule is "code work uses worker/free-verified pool,
escalating to Opus then escalation/fable-first combo."  Opus through cc/
subscription is NOT pay-per-token (absolute rule, 2026-11-20).  A tier-1 route
exhausting its judged-failure budget should escalate to tier-0 subscription/free
routes, not to paid (METERED) frontier routes.

## What Changes

- `verdict/orchestration/contracts.py`: add
  `escalation_capacity_classes: frozenset[CapacityClass]` field to
  `TaskRequirements` (empty = no restriction, used for non-escalated selection).
  Register `"escalation_capacity_excluded"` reason (implicit via eligibility).
- `verdict/orchestration/eligibility.py` (`_assess`): after the tier ceiling
  check passes, apply the capacity class restriction: routes whose
  `CapacityClass` is not in `escalation_capacity_classes` fail at
  `TASK_ELIGIBLE` with reason `"escalation_capacity_excluded"`.  Empty set
  means no restriction (preserves non-escalated behaviour).
- `verdict/orchestration/runtime.py`:
  - `RuntimePolicy`: add
    `escalation_tier0_capacity_classes: frozenset[CapacityClass]` defaulting to
    `{SUBSCRIPTION, FREE}`.
  - `NodeRun`: add `escalation_capacity_classes: frozenset[CapacityClass]`.
  - `_record_failure`: split the existing `else` branch (was t==1 and t==0
    both emit exhausted) into `elif t == 1` (set ceiling=0 + capacity classes)
    and `else` (t==0, emit exhausted from `_record_failure` as before).
  - `_drive`: pass `escalation_capacity_classes=run.escalation_capacity_classes`
    to the escalation `TaskRequirements`; emit `"escalation_exhausted"` from
    `_drive` when `_esc_ceiling == 0 and _esc_choice is None` (lazy check —
    no pre-scan); add `capacity_classes_allowed` to the `"escalation"` event
    for the 1→0 step.
- `tests/test_orch_escalation.py`: extend `EscalationSelector` to accept
  3-tuples with optional `CapacityClass` per route and filter by
  `escalation_capacity_classes`; add four failing-first tests.
- `tests/test_orch_eligibility.py`: add two tests for the
  `escalation_capacity_classes` filter in `_assess`.

## Invariants Preserved

- Admission narrows only: escalation never re-admits a route.
- Non-escalated selection is unchanged: empty `escalation_capacity_classes` =
  no restriction.
- METERED and UNKNOWN-capacity tier-0 routes are NEVER selected through
  escalation; only SUBSCRIPTION and FREE qualify.
- Budget is bounded: when no tier-0 candidate qualifies, `escalation_exhausted`
  is emitted and the runtime falls back to the full admitted set.
- Strength is from measured facts (`capability_tier` in metadata), never names.
- Existing `escalation_exhausted` from t==0 (`_record_failure`) is preserved.

## Impact

`verdict/orchestration/{contracts,eligibility,runtime}.py`,
`tests/test_orch_escalation.py`, `tests/test_orch_eligibility.py`.

Real-story runs where tier-1 routes exhaust their judged-failure budget will
now attempt tier-0 subscription routes (e.g. cc/claude-opus-5) before
emitting `escalation_exhausted`, increasing the probability of task completion
at no extra cost.
