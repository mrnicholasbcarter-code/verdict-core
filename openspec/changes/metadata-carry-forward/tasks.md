# Tasks

## 1. Specification

Write the proposal, design and spec delta before touching production code. Verify:
`npx @fission-ai/openspec@1.13.2 validate metadata-carry-forward --strict` and the
vendored validator both pass.

- [x] 1.1 Write proposal.md, design.md, tasks.md and the spec delta
- [x] 1.2 Both OpenSpec validators pass

## 2. Tests first

Write failing tests before the fix. Verify: each new test is red on the unmodified
`store.py` and the failure message names the unmet condition (not an import error).

- [x] 2.1 `test_refresh_without_include_p1_keeps_fresh_p1_ratings`: CLI refresh without `--include-p1` in a temp `VERDICT_HOME` keeps every OpenRouter AA value with its original `fetched_at`, and the skipped source's reason says how many were carried forward
- [x] 2.2 `test_carried_ratings_past_freshness_window_stay_stale_for_planner_band`: ratings carried 13 hours later keep their original `fetched_at` and count for no planner band
- [x] 2.3 `test_repeated_skipped_refreshes_keep_original_time_and_one_note`: two skipped refreshes in a row keep the original `fetched_at` and name the carry-forward once
- [x] 2.4 `test_source_that_ran_is_not_carried_forward`: a source that ran and returned fewer rows loses the missing ones, with no carry-forward note

## 3. Implementation

Apply the fix. Verify: `uv run --frozen python -m pytest -q tests/test_model_metadata.py`
passes, and `test_include_p1_false_skips_openrouter` still passes.

- [x] 3.1 `store.py`: add `_carry_forward_skipped_source_fields` and call it from `refresh_metadata` after `_retain_failed_source_fields`
- [x] 3.2 `docs/guides/model-metadata-store.md`: describe carry-forward under "Invent-never"

## 4. Gates

Verify: `uv run --frozen ruff check . && uv run --frozen ruff format --check .`,
`uv run --frozen mypy verdict --strict`, `uv run --frozen python scripts/check_lint_gates.py`
and `git diff --check` all pass.

- [x] 4.1 Focused tests pass
- [x] 4.2 ruff check, ruff format, mypy --strict
- [x] 4.3 Lint gates (reachability ratchet, OpenSpec validators, open-task check), `git diff --check`

## 5. Live verification

Verify: on installed main, `verdict metadata refresh` without `--include-p1` leaves the
rated-model count unchanged (issue #1300 live check).

- [ ] (post-merge) 5.1 Install merged main, count rated models in `~/.verdict/model-metadata.json`, run `verdict metadata refresh` without `--include-p1`, and confirm the count is unchanged and the output says values were carried forward
