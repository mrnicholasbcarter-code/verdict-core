# Tasks

## 1. Implementation

Verification: `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/lint-autofix-before-gates` exits 0. Test: all 27 tests in `tests/test_repo_gates_runtime.py` and 175 orch tests pass.

- [x] 1.1 Add `"lint_autofix"` to `EVENT_TYPES` frozenset in `verdict/orchestration/contracts.py`.
- [x] 1.2 Remove `_repair_formatting` from `verdict/orchestration/runtime.py`.
- [x] 1.3 Add `_apply_lint_autofix` to `verdict/orchestration/runtime.py` (safe fixes before verification and gates).
- [x] 1.4 Update `_validate` in `verdict/orchestration/runtime.py` to call `_apply_lint_autofix` after ownership check and before `verification_command`.
- [x] 1.5 Add `lint_autofix` event handling in `_node_record` in `verdict/orchestration/receipt.py`.

## 2. Tests

Verification: `uv run python -m pytest -q tests/test_repo_gates_runtime.py tests/test_orch_runtime.py tests/test_orch_review.py tests/test_orch_receipt.py` — 202 tests pass, 0 failures.

- [x] 2.1 Update `test_implement_node_gate_failure_is_correction_feedback`: add `"--fix" not in argv` guard so the proactive autofix call does not consume the mock's `seen["check"]` counter.
- [x] 2.2 Update `test_existing_debt_in_one_gate_does_not_hide_a_new_failure_in_another`: switch "new failure" gate from ruff-format to mypy (proactive autofix now fixes format before the gate sees it).
- [x] 2.3 Update `test_formatting_only_failure_is_repaired_with_the_formatter`: replace `FORMATTER_APPLIED` controller event assertion with `lint_autofix` event assertion.
- [x] 2.4 Update `test_lint_failures_are_never_auto_fixed`: replace trivial `FORMATTER_APPLIED` assertion with assertion that `lint_autofix` event has `changed_files=[]`.
- [x] 2.5 Add `test_lint_autofix_runs_before_verification_and_gate`: track call order through runner; assert autofix calls precede both verify and gate calls.
- [x] 2.6 Add `test_lint_autofix_event_in_node_receipt`: use `EventLog` + `write_run_receipt`; assert `lint_autofix` data in per-attempt node record.
- [x] 2.7 Add `test_ruff_missing_records_event_gates_proceed`: monkeypatch `_resolve_gate_argv` to raise `FileNotFoundError`; assert `lint_autofix` event has `ruff_missing=True` and no crash.
- [x] 2.8 Add `test_autofix_skipped_when_no_ruff_gate`: use mypy-only config; assert `lint_autofix` event has `skipped=True` and `reason="no_ruff_gate"`; assert no ruff calls on node worktree.

## 3. Lint, type checks, and gates

Verification: all commands below exit 0. Test: each command exits 0 with no errors reported.

- [x] 3.1 `uv run ruff check . && uv run ruff format --check .`
- [x] 3.2 `uv run mypy verdict --strict`
- [x] 3.3 `uv run python scripts/check_lint_gates.py`
- [x] 3.4 `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/lint-autofix-before-gates`
- [ ] (post-merge) 3.5 Push through vgate; CI exact-head checks green; squash-merge to main.

## 4. Post-merge live acceptance

Verification: operator runs `verdict orchestrate` on a story that previously failed with F401 or N818 and confirms the attempt completes without a lint gate failure. Test: post-merge live proof recorded in evidence.

- [ ] (post-merge) 4.1 From merged main, run `verdict orchestrate` on a story that includes a worker likely to produce unused imports; confirm no lint gate failure on the first attempt.
- [ ] (post-merge) 4.2 Confirm `verdict doctor` green and no regression in existing orchestrate runs.
