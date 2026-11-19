# Reviewer waits out one short cooldown

## Why

A clean certification rehearsal ended BLOCKED with "independent review ERROR: no independent
reviewer eligible". The only route that met the reviewer floor was 7 seconds from the end of a
60-second cooldown. Workers already wait out a cooldown up to
`RuntimePolicy.max_cooldown_wait_seconds`; the reviewer did not, so one transient failure on the
last independent route ended an otherwise finished run.

## What Changes

- When reviewer selection returns no route, the reviewer waits once per review level for the
  earliest route whose only rejection is a cooldown ending within the policy limit and within the
  remaining review deadline, then selects again. The route is checked by re-assessing it with the
  same requirements at the cooldown's end, so implementer routes, excluded families and pools,
  and routes below the reviewer floor are never waited for.
- The wait uses the runtime's clock and sleep, and is recorded as a `cooldown` event with scope
  `wait`, category `waiting_for_capacity`, `until`, the route as `key`, `phase: review` and the
  review `level`.
- After the wait the route still goes through selection, the launch gate and the pre-dispatch
  re-check. Reviewer attempts stay bounded by `max_reviewer_attempts` and the review deadline.

## Impact

- Code: `verdict/orchestration/review.py`, `verdict/orchestration/runtime.py`.
- No change to the reviewer floor, implementer route, family or pool exclusion, or the rule that a
  skipped or empty review is an error.
