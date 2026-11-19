# Certification preflight screens only planner-eligible tiers

## Why
The certification rehearsal has ended BLOCKED with "planning failed on every
eligible frontier model; last: none eligible" on every run since 2026-10-10.
With an empty `VERDICT_HOME` the model-metadata store is unavailable, so
`identify --screen` orders routes by id. The four routes the preflight (added
by bod-360-rehearsal-free-preflight) checks are all capability tier 3. Two
pass, so the preflight's `passed > 0` check holds, but the frontier planner
needs `max_capability_tier=2` and rejects them as `insufficient_capability`.
The failure artifact reported the reason as `unclassified`, which hid this.

## What Changes
- `verdict identify --screen` gains `--max-capability-tier N` (0 to 3). Routes
  whose capability tier is unknown or above N are skipped without a probe
  (`skipped_unknown_tier`, `skipped_capability_tier`) before the route caps
  apply. The tier comes from the eligibility ladder's own `_capability_tier`.
  Screen rows report `capability_tier`, `supports_tools` and `context_window`.
- The planner floor (tools, 100k context, tier <= 2) becomes one set of
  constants in `verdict.orchestration.contracts`, read by both
  `plan_with_failover` and the rehearsal preflight.
- The rehearsal preflight passes `--max-capability-tier 2` and fails closed
  unless at least one pass meets the planner floor.
- `certification-failure.json` maps `run_finished` reasons to a closed enum
  and carries the preflight counts (`routes_checked`, `passed`,
  `planner_eligible_passed`, `metadata_error`). Raw reason text never enters it.

## Impact
Admission is unchanged: unknown tier stays ineligible and the planner's tier
floor is not lowered. Screens without the flag behave as before. The preflight
stays FREE-only and bounded: 2 routes per provider, and the route cap rises from 4 to
8 because the local empty-home proof planned and then exhausted the worker pool
on the 3 routes 4 slots produced.
