# Spec Delta: Review token-budget scaling

## ADDED Requirements

### Requirement: OCR token budget scales with diff size

`OpenCodeReviewer` SHALL compute the OCR `--max-tokens-budget` from the total byte
size of all files changed between the base and head refs using the formula:
`max(REVIEW_BUDGET_FLOOR, min(REVIEW_BUDGET_CEILING, total_bytes * 20))`.

`REVIEW_BUDGET_FLOOR` is 400,000 tokens. `REVIEW_BUDGET_CEILING` is 4,000,000 tokens.
When no changed files are found (differ error, deleted-only diff, or zero files), the
floor is used and `token_budget_source` is `"default"`.

The budget and its source are stored on `ReviewResult` as `token_budget_used` (int)
and `token_budget_source` (str).

#### Scenario: 50 KB diff yields 1 M token budget

- **WHEN** changed files total 50,000 bytes
- **THEN** `--max-tokens-budget` passed to OCR is 1,000,000
- **AND** `result.token_budget_source` is `"scaled"`

#### Scenario: Floor applied for tiny diff

- **WHEN** changed files total 10,000 bytes (scaled result 200,000 < floor)
- **THEN** `--max-tokens-budget` is 400,000 (floor)

#### Scenario: Ceiling applied for large diff

- **WHEN** changed files total 300,000 bytes (scaled result 6,000,000 > ceiling)
- **THEN** `--max-tokens-budget` is 4,000,000 (ceiling)

#### Scenario: No changed files uses default (floor)

- **WHEN** the differ returns no files
- **THEN** `--max-tokens-budget` is 400,000 (floor)
- **AND** `result.token_budget_source` is `"default"`

---

### Requirement: Explicit token-budget override

An operator MAY supply an explicit token budget via `--review-token-budget N`
(CLI flag) or `VERDICT_REVIEW_TOKEN_BUDGET=N` (environment variable). The flag
wins over the env var; both win over auto-scaling.

A supplied budget MUST be a positive integer no greater than `REVIEW_BUDGET_CEILING`.
An invalid value (non-integer, below 1, above ceiling) SHALL produce a `BLOCKED:`
error before the run starts, and `token_budget_source` is `"flag"` or `"env"`
respectively.

#### Scenario: CLI flag overrides auto-scaling

- **WHEN** `--review-token-budget 600000` is passed and changed files total 50,000 bytes
- **THEN** `--max-tokens-budget` passed to OCR is 600,000 (not the auto-scaled 1,000,000)
- **AND** `result.token_budget_source` is `"flag"`

#### Scenario: Env var sets budget with source label

- **WHEN** `VERDICT_REVIEW_TOKEN_BUDGET=800000` is set and no flag is passed
- **THEN** `--max-tokens-budget` passed to OCR is 800,000
- **AND** `result.token_budget_source` is `"env"`

#### Scenario: Zero budget rejected at construction

- **WHEN** `OpenCodeReviewer` is constructed with `token_budget=0`
- **THEN** `ValueError` is raised with a message mentioning "positive"

#### Scenario: Above-ceiling budget rejected at construction

- **WHEN** `OpenCodeReviewer` is constructed with `token_budget=REVIEW_BUDGET_CEILING + 1`
- **THEN** `ValueError` is raised with a message mentioning "ceiling"

---

### Requirement: Budget recorded in review event and receipt

The review event emitted to `events.jsonl` and the receipt review block SHALL include
`token_budget_used` and `token_budget_source` when non-zero / non-empty. Runs that
predate this change omit these fields; their receipts recompute byte-for-byte.

#### Scenario: Scaled budget appears in ReviewResult

- **WHEN** `OpenCodeReviewer` auto-scales to 1,000,000 tokens for a 50 KB diff
- **THEN** the returned `ReviewResult` has `token_budget_used == 1_000_000`
- **AND** `token_budget_source == "scaled"`

---

### Requirement: Partial review is not a pass

`_empty_review_reason` SHALL return a non-None error string when the OCR output
has `status: "partial"` or `summary.budget_exceeded: true`. A partial review is
treated as an ERROR, not a pass, even when no blocking findings were returned from
the items that were reviewed.

The error detail SHALL include the word `"partial"` or `"budget"` and, when
`summary.total_tokens` is present, that number.

#### Scenario: Partial review with no findings errors

- **GIVEN** OCR returns `status: "partial"`, `budget_exceeded: true`, 0 blocking findings
- **THEN** `result.passed` is `False`
- **AND** `result.status` is `"ERROR"` or `"FAIL"`
- **AND** `result.detail` contains `"partial"` or `"budget"`
