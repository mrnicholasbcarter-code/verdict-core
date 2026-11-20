# Design

## Context

`DagRuntime._validate()` in `verdict/orchestration/runtime.py` runs the
worker's verification command and returns a verdict string. Any non-zero
exit returned `"verification_failed: exit N: <output>"`. The calling
method `_attempt()` then called `_fail()` / `_record_failure()`, which
increments `judged_failure_count`, writes the route into the session
ledger with a "fail" outcome, and potentially marks the route unhealthy
in the health-cache.

This was correct for genuine wrong-output failures but wrong for
environment failures: missing `pytest` (exit 1 + "No module named
pytest"), missing runner binary (exit 127 from `subprocess_runner`'s
`FileNotFoundError` trap), or non-executable runner (exit 126). None of
those are the route's fault.

## Goals / Non-Goals

Goal: `environment_error` is a distinct category that fails the node closed
(BLOCKED), does not charge the attempt to the route, and writes nothing
to the session ledger or health-cache.

Goal: cheap pre-dispatch check: if the runner (argv[0]) cannot be found
before the first executor call, block immediately with attempt=0.

Non-goal: detect all possible environment failures (e.g., disk full,
permission denied on a file the test suite writes). Only the two reliable
signals are detected: exit 126/127 and "No module named MODULE" for `-m`
invocations.

Non-goal: retry environment errors. They are systemic (the venv is broken
for the whole run). A single BLOCK is correct.

## Decisions

### Classification via string prefix

`classify_verify_env_error()` and `check_verify_runner()` live in
`verdict/orchestration/verification.py`, alongside the existing argv
resolution helpers. They return `"environment_error: <reason>"` or `None`.

The `"environment_error:"` prefix is the same contract as the existing
`"verification_failed:"` prefix: callers key on the prefix to distinguish
categories.

### State machine path for `_attempt()`

After `_validate()` returns `"environment_error: …"`, the correct path
through the state machine is:
- Append `FailureClassification(category="environment_error", action="BLOCK",
  cooldown_seconds=0, scope="none")` to failures.
- Emit a `"failure"` event (category="environment_error").
- Call `self._set(run, NodeState.REJECTED, …)` — the only valid non-VALIDATED
  transition from TERMINAL_SUCCESS.
- Return `False` to `_attempt()`.
- In `_drive()`, the existing `action == "BLOCK"` guard then transitions
  REJECTED → BLOCKED.

`_record_failure()` / `_fail()` are NOT called, so `judged_failure_count`
is not incremented and the session ledger/health-cache are not written.

### Pre-dispatch check

`_pre_dispatch_env_check()` checks `check_verify_runner(node.verification_command)`
before the `while True:` loop in `_drive()`. If the runner is missing,
it emits a `"failure"` event with `attempt=0`, appends the classification
to failures, and transitions directly to BLOCKED. The executor is never
called.

Owned-file parent directory presence is NOT checked here: workers routinely
create new directories, so a missing directory is not an environment error.

## Risks / Trade-offs

The `-m MODULE` check matches on the exact module name: `"No module named
pytest"` matches `pytest` from `python -m pytest`. This is intentional:
a `ModuleNotFoundError` traceback from within the test suite (a genuine
wrong-output failure) would name a different module, not `pytest` itself.
Edge case: if the test suite imports `pytest` and raises `ModuleNotFoundError`
for `pytest` as a string, it would be misclassified. This is considered
extremely unlikely in practice.

## Test strategy

Three acceptance tests in `tests/test_orch_runtime.py`:

1. `test_missing_pytest_spends_one_attempt_and_fails_closed`:
   runner returns `(1, "No module named pytest")` → category is
   `"environment_error"`, node is BLOCKED, attempt==1, no health-cache
   write (checked via absence of `SessionLedger.record_outcome` call).

2. `test_exit_127_during_verify_is_env_error`:
   runner passes pre-dispatch (python is found), but during `_validate()`
   the mock returns 127 → `environment_error`.

3. `test_pre_dispatch_check_missing_runner_zero_attempts`:
   runner is not on PATH → pre-dispatch blocks at attempt==0, executor
   never called.
