# Tasks

- [x] Add pool cooldown and expiry regressions for planner and worker roles.
- [x] Add explicit pool exclusions to the existing task requirements.
- [x] Reuse pool_of for dispatch, assessment, and failure cooldowns.
- [x] Test free-first selection against the planner capability floor.
- [x] Add repair and summary-file feedback regressions.
- [x] Run focused tests and touched-file lint/format when load1 < 6.

## Evidence (truth audit 2026-10-13)

Focused eligibility, feedback-selection and planner-fallback tests pass on `ff8b4016`; merged in #811.
