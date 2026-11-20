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

- `verdict/orchestration/verification.py`: new function
  `classify_verify_env_error()` detects exit codes 126/127 and missing
  `-m MODULE` as environment errors.
- `verdict/orchestration/runtime.py`: `_validate()` calls
  `classify_verify_env_error` and returns `"environment_error: …"` before
  `"verification_failed: …"`. `_attempt()` handles `"environment_error:"`
  prefix by appending a `FailureClassification(category="environment_error",
  action="BLOCK", cooldown_seconds=0, scope="none")`, then transitioning to
  REJECTED (not incrementing `judged_failure_count`). `_drive()` applies the
  same BLOCK action via its existing logic.
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
  node is failed closed with zero route-level impact (no health-cache
  write, no session ledger entry, no escalation).

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
3. When a binary is completely absent from PATH, `subprocess_runner`'s
   FileNotFoundError → (127, ...) path causes `classify_verify_env_error`
   to classify the failure as `environment_error` at attempt=1.

## Deviation from planned scope

A runner pre-dispatch check (checking argv[0] resolves before dispatch)
was dropped. It conflicts with the BOD-70 regression contract in
`tests/test_orch_verify_command.py::test_runtime_verify_missing_binary_rejected_not_blocked`,
which requires the verify event to be emitted with exit_code=127. It also
fires for single-character argv[0] values produced when a string
verification_command is converted to tuple in WorkNode.__post_init__. The
`classify_verify_env_error()` classification at attempt 1 covers all the
cases the story requires, including the missing-binary case via exit 127.
