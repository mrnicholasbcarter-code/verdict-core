# Tasks

- [x] Add pool cooldown and expiry regressions for planner and worker roles.
- [x] Add explicit pool exclusions to the existing task requirements.
- [x] Reuse pool_of for dispatch, assessment, and failure cooldowns.
- [x] Test free-first selection against the planner capability floor. (Obsolete per BOD-342 2026-10-21: price-free selection replaced free-first ordering; see superseded note in specs/orchestration/spec.md.)
- [x] Add repair and summary-file feedback regressions.
- [x] Run focused tests and touched-file lint/format when load1 < 6. (done 2026-10-21 on main 9c471e07: 283 passed; ruff clean)
