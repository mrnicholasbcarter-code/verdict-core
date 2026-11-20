# Capability escalation after repeated judged failures

## Why

Run 20261120T013732Z (real-story-6) always picked Haiku-tier routes
(`cc/claude-haiku-4-5-20251001`, `cc/claude-haiku-5-5`) because both have
`capability_tier=3` from the name-based classifier fallback, `slack=0` under a
`max_capability_tier=3` node, and therefore always win the cheapest-qualified
ranking.  After two rehydrated verification failures on Haiku the run exhausted
those routes and blocked — it never escalated to Sonnet.  Run 20261119T225440Z
(real-story-5) landed on Sonnet only because Haiku routes were already cooled
from an earlier run.

The root cause (eligibility.py, `_rank_key` tuple, element 7) is that
`slack = max_capability_tier - tier` → smaller slack wins → Haiku's slack of 0
always beats Sonnet's slack of 1 when both pass the capability floor and no
escalation signal exists.

Capability escalation fixes this: after K judged failures the runtime narrows
the selection to routes strictly stronger than the failing tier, using only
`capability_tier` from metadata/classifier facts — never from model names.

## What Changes

- `verdict/orchestration/contracts.py`: add `escalation_max_tier: int | None`
  field to `TaskRequirements`; register `"escalation"` in `EVENT_TYPES`.
- `verdict/orchestration/eligibility.py` (`_assess`): apply
  `escalation_max_tier` as an additional tier ceiling; routes whose tier exceeds
  the ceiling fail at `TASK_ELIGIBLE` with reason `"below_escalation_floor"`.
- `verdict/orchestration/runtime.py`:
  - Add `judged_failure_count`, `last_selected_tier`, `escalation_tier_ceiling`
    to `NodeRun`.
  - In `_record_failure`: after K=2 judged failures, tighten
    `escalation_tier_ceiling` to `last_selected_tier - 1`.
  - In `_attempt`: two-phase selection — try with ceiling first; if no route
    is found, fall back to the full admitted set so the budget stays bounded.
    Unknown-tier routes are excluded from the escalation path
    (`unknown_capability_escalating`) so free-first workers escalate correctly.
  - Emit `"escalation"` event and append to `run.history` when escalation fires.
- `tests/test_orch_escalation.py`: four failing-first tests covering the happy
  path, exclude-routes respected, fallback when no stronger route, and
  non-judged failures not counted.

## Invariants Preserved

- Admission narrows only: escalation reorders/narrows within the already-admitted
  set via `exclude_routes` and `escalation_max_tier`; it never re-admits a route.
- Opaque routes (`auto/*`, `combo/*`) are never touched.
- Budget is bounded: when no escalated route exists the fallback ensures an
  eligible route is still selected within the existing attempt cap.
- Strength is always from measured facts (`capability_tier` from
  metadata/classifier), never from model name strings.

## Impact

`verdict/orchestration/{contracts,eligibility,runtime}.py`,
`tests/test_orch_escalation.py`. Real-story runs that previously exhausted
all Haiku routes after repeated failures will now escalate to a Sonnet-tier
route on the next attempt.
