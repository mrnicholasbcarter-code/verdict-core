# Tasks

## 1. Specification

- [x] 1.1 Write proposal.md, design.md, tasks.md and the spec delta with scenarios
- [x] 1.2 Verify both validators pass: `npx --yes @fission-ai/openspec@1.13.2 validate --all --strict --no-interactive` and `uv run python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/contested-proof-rescreen`

## 2. Tests first

- [x] 2.1 Verify `test_acceptance_failing_all_listed_produces_rescreen_hint` is red without the fix
- [x] 2.2 Verify `test_acceptance_failing_one_attempt_then_passing_does_not_produce_rescreen_hint` is green without the fix
- [x] 2.3 Verify `test_rescreen_requested_bypasses_skipped_fresh_and_probes` is red without the fix
- [x] 2.4 Verify `test_reprobe_budget_is_reserved_before_chat_phase` is red without the fix

## 3. Implementation

- [x] 3.1 `measure()` returns `failed_all_listed`; `eligibility_check` queues rescreen hints after the tool check without writing health or proof
- [x] 3.2 `screen_models` `decide()`: bypass `skipped_fresh` when `route_id in requested`
- [x] 3.3 `Prober.run_once` reserves `_reprobe_reserved`; `_run_chat_phase` respects it with an early return when `chat_max <= 0`

## 4. Verification

- [x] 4.1 Focused tests pass: `uv run python -m pytest -q tests/test_acceptance_toolcheck.py tests/test_acceptance_red_fixes.py tests/test_identify_screen.py tests/test_prove_at_rest_agentic_budget.py`
- [x] 4.2 `uv run ruff check . && uv run ruff format --check .` clean
- [x] 4.3 `uv run mypy verdict --strict` clean
- [x] 4.4 Full suite green: `uv run python -m pytest -q -n auto -p no:cacheprovider`
- [ ] (post-merge) 4.5 On installed main, a reprobe of a named free route runs, and a listed route that fails acceptance is re-screened at the next scheduled screen
