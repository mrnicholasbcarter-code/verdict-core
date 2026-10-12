# Tasks

## 1. Bounded planning operations
- [x] 1.1 Add immutable owner-bound lease state and supervisor first-read validation;
  verify fake-clock 600-second planner/240-second stall and expiry regressions.
- [x] 1.2 Wrap initial and repair executor calls with fixed whole-call timeout and
  exact finally lifecycle; verify planner tests and scope-error regression.
- [x] 1.3 Document caps, deadline precedence and unsupported phases in design.md.

## 2. Detached ownership
- [x] 2.1 Register intent before spawn and detached session before exec under a
  stable lock; verify revoked-generation payload never executes.
- [x] 2.2 Revoke before kill and verify contained cleanup before restart; verify short
  local detached kill/block and stale-PID/no-signal regressions plus fixture cgroup checks.
- [x] 2.3 Document unresolved intent fail-closed behavior and deliberate cgroup-escape trust limit.

## 3. Integration validation
- [x] 3.1 Run focused tests, Ruff, strict mypy and OpenSpec validation; record results.
- [x] 3.2 Obtain independent scoped architecture source review of actual diff;
  controller-safety-review confirmed PASS. This is not kernel proof or release approval.
