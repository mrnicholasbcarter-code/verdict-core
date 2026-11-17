# Tasks

## 1. Implementation

- [x] 1.1 Add the full-coverage screening requirement and scenarios.
- [x] 1.2 Update scheduled screen caps, cadence, and timeout. Verification: focused unit-rendering tests assert exact generated lines.
- [x] 1.3 Add unit rendering regressions. Verification: run the touched test file and visual-system regression.
- [x] 1.4 Run focused and repository quality gates. Verification: execute the commands listed in the parent task.
- [x] (post-merge) Reinstall the timer with verdict setup and record one scheduled run's unit, routes_checked, stopped_reason and skipped_cap count.
  - 06:00: verdict-identify-screen.service, routes_checked 40, stopped_reason route_cap, outcomes pass 11 / fail 26 / auth_failed 3 / skipped_cap 523. 18:00: routes_checked 39, stopped_reason wall_cap, model_calls 73.