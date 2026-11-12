# Spec Delta

## Purpose

Defines the attempt and checker ledger: one terminal row per sent attempt plus independently-verified checker rows, with crash-safe append and autodev lineage.

## ADDED Requirements

### Requirement: One terminal row per sent model attempt

Every model attempt that was actually sent (orchestration dispatch, HTTP relay, or autodev planner/executor turn) SHALL produce exactly one `record_kind="terminal"` ledger row. Local and mechanical attempts SHALL be excluded. A terminal row SHALL NOT carry `quality_credit`.

#### Scenario: Orchestration terminal rows
- Proof: `tests/test_every_attempt_links_verified_checker_without_rewriting_receipts`

#### Scenario: HTTP relay terminal rows
- **THEN** a buffered, streamed or denied-before-send relay attempt writes one terminal row, mapped by case: buffered 429 -> `quota_429`, 401/403 -> `auth_error`, 5xx -> `provider_5xx`; stream SUCCESS -> `accepted`/`success` (protocol completion only, `quality_credit=None`); stream FAILED -> `failed`/`other`; stream INCOMPLETE -> `partial`/`stream_drop`; ABRUPT_EOF/protocol/transport error -> `failed`/`stream_drop` or `timeout`; ABORTED/cancelled -> `abandoned`; identity mismatch -> `failed`; denial before send -> `abandoned`.

#### Scenario: Autodev lineage
- **THEN** every planner and executor row carries `attempt_run_id`, `decision_id` (the execution-path decision digest), `node_id` (`planner` or the unit id), and a monotonically increasing attempt id across repair/retry turns, plus the executor's `effective_parameters` (temperature, max_tokens).

### Requirement: Checker rows carry quality credit only when independently verified

A `record_kind="checker_result"` row SHALL be the only row kind that may carry `quality_credit`. `quality_credit` SHALL be required when the row is a checker_result with a `checker_digest`, SHALL be one of `{0, 0.5, 1}`, and SHALL match the outcome class (1 accepted, 0.5 partial, 0 failed). Operational failure classes (quota, auth, timeout, stream_drop, 5xx, cooldown, tool, other) SHALL NOT carry credit. A self-claimed checker_result with credit and no digest SHALL be rejected (exit 2 from `outcome.record`).

#### Scenario: Validation
- **THEN** `build_attempt_record` raises on a credit/outcome mismatch, on credit without a digest, and on an operational class carrying credit.

### Requirement: Checker rows are produced only from an independent verification event

A checker_result row SHALL be written only where the runtime's own `verify` event ran after an ok terminal and before the next dispatch, with `checker_digest` equal to the receipt's `events_digest`. A quota failure SHALL get no checker row.

#### Scenario: Independent join
- **THEN** `write_run_receipt` appends terminal and checker rows after `receipt.json`; a ledger write error is a warning only and never blocks the receipt. Re-writing the same receipt is byte-identical: no new rows, bundle unchanged.

### Requirement: Checker-join accepts a row only under full verification

`load_checked_attempts(run_dir, store_dir, as_of)` SHALL join a ledger row to a run only when all hold: the receipt verifies; the digest is equal; the key exists in the run; the row is byte-equal to the recomputed row; the verified check time (from the run's own events, never the ledger row's `ts`) is at or before `as_of`; it is the first row for that key. Every other case SHALL be rejected with a named reason: `checker_digest_mismatch`, `scope_mismatch`, `tampered`, `future`, or `duplicate`.

#### Scenario: Rejection reasons
- **THEN** `build_outcome_shadow` reports `checker_join` counts by reason; a forged `checker_digest` is written but never joins.

### Requirement: Ledger append is crash-safe but not crash-atomic

The ledger writer SHALL take a `flock`, loop `os.write` until all bytes are written (zero bytes raises `OSError`), then `fsync`. It SHALL NOT claim crash atomicity: a process killed mid-write may still leave a torn row, which the join logic (not the writer) is responsible for rejecting. `append_new_attempt_records` SHALL be idempotent per `(run, node, attempt, request, kind, digest)`.

#### Scenario: Concurrent writers
- **THEN** 4 concurrent writer processes each writing 50 rows produce 200 whole rows with no torn row under the lock.

### Requirement: Legacy rows load unscored

`AttemptRecord`'s 11 scoring fields SHALL default to `None`, so a legacy row (written before this change) loads successfully and contributes no quality credit.

#### Scenario: Legacy compatibility
- **THEN** a legacy row with no scoring fields set loads with `record_kind=None` and `quality_credit=None`.
