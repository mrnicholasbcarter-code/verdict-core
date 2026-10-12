# Controller operation safety

## Why
A quiet planner bounded at 600 seconds was killed by the supervisor's 240-second
progress watchdog. Detached Prime sessions also survived controller termination.
This correction separates bounded waiting from progress and makes worker cleanup
a prerequisite to controller replacement.

## What Changes
- Publish owner-bound, fixed initial and repair planning operation deadlines.
- Observe bounded waiting without claiming progress or model health.
- Keep the overall supervisor deadline authoritative even for late completion.
- Register supervised detached Prime sessions before payload execution, with
  durable pending-launch intent and generation revocation.
- Verify owned worker cleanup before replacement; unknown ownership blocks.

## Capabilities
### New Capabilities
- `orchestration-controller-operation-safety`: bounded operation waiting and
  ownership-safe supervised Prime cleanup.
### Modified Capabilities
None. Existing review, receipt, admission and integration contracts are unchanged.

## Impact
Supervisor, Prime executor, a reusable operation/ownership module and the minimal
planner call bridge. Supervised execution requires Linux `/proc` and pidfds.
No dependencies, settings, proof gates or provider admission policy change.
