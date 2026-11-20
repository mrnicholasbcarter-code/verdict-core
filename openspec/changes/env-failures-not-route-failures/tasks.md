# Tasks

## 1. Implementation

- [x] 1.1 Add `classify_verify_env_error()` to
      `verdict/orchestration/verification.py`
- [x] 1.2 Update `_validate()` in `runtime.py` to return `"environment_error: …"`
      before `"verification_failed: …"` for env failures
- [x] 1.3 Add `environment_error` branch in `_attempt()` that bypasses
      `_record_failure()` / session ledger / health-cache
- [x] 1.4 Add `environment_error` classification to `recovery.py`
- [x] 1.5 Add `"environment_error"` to `_NON_AVAILABILITY_ERRORS` in
      `session_feedback.py`
- [x] 1.6 Add `"environment_error": "env_error"` to reason map in `receipt.py`
- [x] 1.7 Add `"environment_error": EvaluationFailureClass.CONFIGURATION` in
      `verdict/evaluation.py`
- [x] 1.8 Add 3 acceptance tests in `tests/test_orch_runtime.py`
- [x] 1.9 Run focused `tests/test_orch_runtime.py`, `ruff check`, `ruff
      format --check`, `mypy --strict verdict`
- [x] 1.10 Run `uv run python scripts/check_lint_gates.py`; push with vgate
- [ ] (post-merge) 1.11 CI exact-head checks green on PR head; squash-merge
      to main

## 2. Live checks

- [ ] (post-merge) 2.1 From installed merged main, confirm that an orchestrate
      run in a venv without pytest classifies the failure as `environment_error`
      and the route health-cache is unchanged; record raw output
- [ ] (post-merge) 2.2 Confirm that the node transitions to BLOCKED with
      `attempt == 1`, not to FAIL_CLOSED via the escalation budget path; record
      raw output
