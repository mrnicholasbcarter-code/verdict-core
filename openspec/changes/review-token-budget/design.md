# Design

## Context

OCR's `--max-tokens-budget` is consumed by the aggregate of all reviewed items. A
200 KB diff requires ~4 M tokens; the previous hard-coded 400 k was 10x too low for
real stories. The fix must be deterministic (same inputs → same budget), bounded
(operator-reviewable ceiling), and backward-compatible (older receipts still verify).

## Goals / Non-Goals

Goals: scale the OCR budget to the actual diff size; prevent partial reviews from
passing; let operators override when needed.

Non-goals: changing admission, routing, proof rules, or OCR's internal behaviour.
No new live calls in tests.

## Decisions

- **Multiplier = 20.** Calibrated from run `20261120T054320Z`:
  200 KB * 20 = 4,000,000 (at ceiling). 50 KB * 20 = 1,000,000 (mid-range). Both
  match observed reviewer token consumption from real stories.
- **Floor = 400,000.** Preserves the original behaviour for tiny or empty diffs.
- **Ceiling = 4,000,000.** Conservative upper bound; prevents runaway cost on
  unusually large diffs while covering the known worst case.
- **Source label.** `token_budget_source` is `"scaled"` (auto), `"flag"` (CLI),
  `"env"` (environment variable), or `"default"` (floor, no changed files).
  Operators can distinguish override from auto-computed in the receipt.
- **Backward compat.** `token_budget_used` and `token_budget_source` are added to
  the receipt review block only when non-zero/non-empty, following the same pattern
  as `findings_lost`. Existing receipts recompute byte-for-byte and still verify.
- **Partial veto position.** The `status: partial` / `budget_exceeded: true` check
  is placed after the skipped/empty-selected vetoes but before the positive-signal
  checks in `_empty_review_reason`. A partial result with no blocking findings
  cannot masquerade as PASS.

## Risks / Trade-offs

- Scaling from file bytes is a proxy for token count; large binary files could
  over-estimate. Deleted files (OSError on stat) are silently skipped, which
  under-estimates for large deletions. Both cases fall back to floor or ceiling,
  which are safe bounds.
- An explicit override above the ceiling is rejected at startup, not silently capped,
  so operators get a clear error rather than surprising behaviour.

## Security / trust implications

No new API calls. The budget value comes from local `stat()` calls on the working
tree; it cannot be injected via diff content. The env var `VERDICT_REVIEW_TOKEN_BUDGET`
is an integer with explicit validation; non-integer values and out-of-range values are
rejected with a clear error before any review call is made.

## Routing / context / memory implications

No routing, context-pack or memory change. `ReviewResult` gains two new fields that
are advisory/informational only; they do not affect the `passed` property.

## ADR impact

None. The partial-review veto reinforces ADR-0001 invariant "skipped or empty review
is an error, not a pass" by extending it to cover `status: partial`.

## Migration / rollback

Revert `verdict/orchestration/review.py`, `contracts.py`, `runtime.py`, `receipt.py`,
and `cli.py`. No stored state changes shape; existing `events.jsonl` files are
unaffected.
