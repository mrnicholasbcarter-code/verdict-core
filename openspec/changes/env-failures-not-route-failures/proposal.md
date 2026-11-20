# Proposal: environment failures are not route failures

## Why

Real-story attempt 7 (2026-11-20) burned 4 attempts because the worktree
venv lacked pytest. Each attempt was classified `verification_failed`,
counted toward the escalation budget, and recorded a "fail" against
Sonnet routes that had done nothing wrong. The route health-cache and
session ledger held incorrect evidence that degraded future routing.

The root cause is that `verification_failed` conflates two distinct
failure modes:
1. The route produced wrong output (the route is at fault).
2. The verification runner or module was missing from the environment
   (the environment is at fault).

Mode 2 should never be charged to the route. It wastes the attempt budget,
poisons route health evidence, and blocks further escalation before the
environment problem is even surfaced.

## What Changes

- `verdict/orchestration/verification.py`: two new functions.
  `classify_verify_env_error()` detects exit codes 126/127 and missing
  `-m MODULE` as environment errors. `check_verify_runner()` resolves the
  runner (argv[0]) before dispatch as a cheap pre-dispatch guard.
- `verdict/orchestration/runtime.py`: `_validate()` calls
  `classify_verify_env_error` and returns `"environment_error: …"` before
  `"verification_failed: …"`. `_pre_dispatch_env_check()` checks the runner
  and gates the node BLOCKED with zero attempts if the runner is missing.
  `_attempt()` handles `"environment_error:"` prefix by appending a
  `FailureClassification(category="environment_error", action="BLOCK",
  cooldown_seconds=0, scope="none")`, then transitioning to REJECTED
  (not incrementing `judged_failure_count`). `_drive()` applies the same
  BLOCK action via its existing logic.
- `verdict/orchestration/recovery.py`: classifies `error.startswith(
  "environment_error")` → `FailureClassification(category=
  "environment_error", action="BLOCK", cooldown_seconds=0, scope="none")`.
- `verdict/orchestration/session_feedback.py`: adds `"environment_error"`
  to `_NON_AVAILABILITY_ERRORS` so no health-cache write is made.
- `verdict/orchestration/receipt.py`: maps `"environment_error"` →
  `"env_error"` in the reason map for receipt encoding.
- `verdict/evaluation.py`: maps `"environment_error"` →
  `EvaluationFailureClass.CONFIGURATION`.

## Capabilities

### New Capabilities

- `env-failures-not-route-failures`: when a verification command fails
  because the runner was missing (exit 126/127) or a `-m MODULE` target
  is not installed, the failure is classified `environment_error`, the
  node is failed closed with zero attempts charged to the route, and no
  session ledger or health-cache entries are written for that route.
- `pre-dispatch-env-check`: before the first attempt on any node, the
  verification command's runner (argv[0]) is resolved; if it cannot be
  found, the node transitions directly to BLOCKED with no executor call
  and attempt=0.

### Modified Capabilities

None. `verification_failed` classification is unchanged for genuine
wrong-output failures.

## Impact

`verdict/orchestration/verification.py`, `runtime.py`, `recovery.py`,
`session_feedback.py`, `receipt.py`, `verdict/evaluation.py`, and
`tests/test_orch_runtime.py`. No schema changes, no new environment
variables, no dependency changes.

## Acceptance criteria

1. When `subprocess_runner` returns `(1, "No module named pytest")` for
   `python -m pytest`, the node is BLOCKED, `attempt == 1`, and
   `FailureClassification.category == "environment_error"` appears in
   the failures list. No health-cache write.
2. When `subprocess_runner` returns `(127, "command not found")` during
   `_validate()`, the node is BLOCKED with
   `FailureClassification.category == "environment_error"`. No reassignment.
3. When the runner (argv[0]) is not on PATH, `_pre_dispatch_env_check()`
   blocks the node at `attempt == 0` before the executor is called.
