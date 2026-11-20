# Tasks

## 1. Implementation

- [x] 1.1 Add `REVIEW_BUDGET_FLOOR`, `REVIEW_BUDGET_CEILING`, `_BUDGET_BYTES_TO_TOKENS` constants and `_changed_file_total_bytes`, `_scaled_token_budget` helpers to `verdict/orchestration/review.py`
- [x] 1.2 Add `_compute_budget` method and update `OpenCodeReviewer.__init__` to accept `token_budget: int | None` and `token_budget_source: str`; validate at construction
- [x] 1.3 Update `_review_once` to call `_compute_budget` and pass budget+source through to `_interpret`
- [x] 1.4 Add partial veto (`status: partial` / `budget_exceeded: true`) in `_empty_review_reason`
- [x] 1.5 Add `token_budget_used: int` and `token_budget_source: str` to `ReviewResult` in `verdict/orchestration/contracts.py`
- [x] 1.6 Emit `token_budget_used`/`token_budget_source` in the review event in `verdict/orchestration/runtime.py`
- [x] 1.7 Pass through budget fields in `_review_block` in `verdict/orchestration/receipt.py`
- [x] 1.8 Add `--review-token-budget` CLI arg and `VERDICT_REVIEW_TOKEN_BUDGET` env var in `verdict/orchestration/cli.py`
- [x] 1.9 Document `VERDICT_REVIEW_TOKEN_BUDGET` and `--review-token-budget` in `docs/CONFIGURATION.md`
- [x] 1.10 Write failing-first tests in `tests/test_review_token_budget.py` (13 tests); create `tests/fixtures/ocr/sample-partial.json`
- [x] 1.11 Update `_wrap_first_as` in `tests/test_orch_review.py` to pass through new `_interpret` arguments
- [x] 1.12 Run `ruff check`, `ruff format --check`, `mypy --strict`, focused tests (188 passed), `check_reachability.py --ratchet-base origin/main` (0 new findings), both OpenSpec validators pass
- [ ] (post-merge) 1.13 Push through vgate; CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

To verify: run `verdict orchestrate` on a story with known diff size from merged main; inspect the receipt with `verdict run-receipt <run>`.

- [ ] (post-merge) 2.1 From installed merged main, run `verdict orchestrate` on a story with ~200 KB diff; confirm the receipt shows `token_budget_used` >= 1_000_000 and `status: PASS` (or `ERROR` only for genuine failures, not budget exhaustion)
- [ ] (post-merge) 2.2 Confirm `verdict orchestrate --review-token-budget 500000` overrides the scaled value and records `token_budget_source: flag` in the receipt
