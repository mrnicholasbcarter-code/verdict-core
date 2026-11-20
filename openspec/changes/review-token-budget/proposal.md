# Review token-budget scaling

## Why

`verdict orchestrate` runs OCR (open-code-review) with a fixed `--max-tokens-budget` of
400,000 tokens. Live evidence from run `20261120T054320Z` shows this is insufficient for
real diffs: 4 files totalling ~200 KB required ~4 M tokens; with the fixed 400 k ceiling,
2 of 4 items failed with `status: partial` / `budget_exceeded: true`. Because
`_empty_review_reason` did not catch `status: partial`, the partial result slipped through
as PASS. Both gaps must be closed.

## What Changes

- **Budget scaling** (`verdict/orchestration/review.py`): replace the fixed 400 k constant
  with a deterministic formula: `max(400_000, min(4_000_000, total_bytes * 20))`.
  `total_bytes` is the sum of byte sizes of all files returned by `git diff --name-only`.
  When no changed files are found (differ error, or zero files) the floor (400 k) is used.
  Constants `REVIEW_BUDGET_FLOOR` and `REVIEW_BUDGET_CEILING` are exported.

- **CLI and env override** (`verdict/orchestration/cli.py`):
  `--review-token-budget N` and `VERDICT_REVIEW_TOKEN_BUDGET` allow operators to supply an
  explicit budget. Invalid values (non-integer, below 1, above ceiling) produce a clear
  `BLOCKED:` error. The flag wins over the env var; both win over auto-scaling.

- **Budget recorded** (`verdict/orchestration/contracts.py`,
  `verdict/orchestration/runtime.py`, `verdict/orchestration/receipt.py`):
  `ReviewResult` gains `token_budget_used: int` and `token_budget_source: str`
  (`"scaled"`, `"flag"`, `"env"`, or `"default"`). The review event and receipt review
  block include both fields when non-zero / non-empty, so older receipts still verify.

- **Partial veto** (`verdict/orchestration/review.py`): `_empty_review_reason` now returns
  an error for `status: partial` or `budget_exceeded: true`. A partial review is not a
  pass: some files were never reviewed.

- **Failing-first tests** (`tests/test_review_token_budget.py`): 13 tests covering scaling
  formula, floor/ceiling, explicit override, source label propagation, validation, and the
  partial-review regression guard.

- **Docs** (`docs/CONFIGURATION.md`): `VERDICT_REVIEW_TOKEN_BUDGET` and
  `--review-token-budget` added to the orchestrate configuration section.

## Capabilities

### New Capabilities

- `review-token-budget-scaling`: OCR token budget auto-scales from diff size with floor/ceiling, operator override via flag and env var, partial review veto.

### Modified Capabilities

- None.

## Impact

`verdict/orchestration/review.py`, `verdict/orchestration/contracts.py`,
`verdict/orchestration/runtime.py`, `verdict/orchestration/receipt.py`,
`verdict/orchestration/cli.py`, `docs/CONFIGURATION.md`,
`tests/test_review_token_budget.py`, `tests/fixtures/ocr/sample-partial.json`.
No live API calls added; no existing test changed except `test_orch_review.py`
(`_wrap_first_as` helper updated to pass through new `_interpret` arguments).

## Evidence boundary

KNOWN: live run `20261120T054320Z` shows `status: partial`, `budget_exceeded: true`,
`total_tokens: 437000` against a 400 k budget with ~200 KB of diff files. KNOWN:
`_empty_review_reason` only checked `"skipped"` status, not `"partial"`. KNOWN:
`_every_item_failed` requires all selected items to fail, so 2-of-4 partial passes.
INFERRED: multiplier 20 covers the observed worst case (200 KB * 20 = 4 M at ceiling).
NOT AVAILABLE until a live run: whether the ceiling is sufficient for diffs larger
than 200 KB.

## Acceptance criteria

- Budget scales from diff bytes: 50 KB → 1 M, floor at tiny, ceiling at large (test).
- Explicit override wins over auto-scaling; source label is `"flag"` or `"env"` (test).
- Zero and above-ceiling budgets raise `ValueError` at construction (test).
- Partial review (budget_exceeded=true) is not a pass; detail contains token count (test).
- `token_budget_used` and `token_budget_source` appear in `ReviewResult` (test).
- On merged main, `verdict orchestrate` on a large diff records budget >= floor in receipt.
