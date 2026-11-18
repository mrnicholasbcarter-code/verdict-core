# Spec Delta

## Purpose

`verdict metrics` computes the BOD-8 benchmark numbers (accepted tasks, dollars, accepted tasks per dollar, free share) only from digest-verified run receipts. Receipt verification agrees with receipt construction on which run id names a run.

## ADDED Requirements

### Requirement: Metrics SHALL count only verified receipts

`verdict metrics` SHALL verify each receipt with `verify_run_receipt` before using it. A receipt with any verification problem SHALL be skipped, reported with its reason, and contribute nothing to any number.

#### Scenario: Tampered receipt is refused
- **WHEN** a run's `events.jsonl` no longer matches the receipt's `events_digest`
- **THEN** the run is listed under `skipped_runs` with a `verification failed` reason and adds no tasks, dollars or attempts

### Requirement: Accepted tasks SHALL require a recomputed COMPLETE outcome

An accepted task SHALL be an implement or integrate node of a run whose outcome, recomputed by `completion_verdict` from the receipt, is COMPLETE. The stored `outcome` field SHALL NOT be trusted. A CANCELLED run, a run with failed or missing OpenSpec conformance, and any BLOCKED run SHALL contribute zero accepted tasks.

#### Scenario: Cancelled run
- **WHEN** a receipt has validated nodes and a PASS review but `claimed_outcome` is CANCELLED
- **THEN** its accepted tasks are 0 and its outcome is CANCELLED

#### Scenario: Failed OpenSpec conformance
- **WHEN** a receipt carries an `openspec` block whose conformance result is not PASS
- **THEN** its accepted tasks are 0 and its outcome is BLOCKED

#### Scenario: Edited outcome field
- **WHEN** a BLOCKED receipt's `outcome` field is edited to COMPLETE
- **THEN** its accepted tasks are still 0

#### Scenario: Complete run
- **WHEN** a receipt's recomputed outcome is COMPLETE with n implement/integrate nodes
- **THEN** its accepted tasks are n

### Requirement: Unknown cost SHALL never be reported as zero

Dollars SHALL be the sum of `usage.cost_usd` over model attempts. When any model attempt has no usage or a null or non-numeric cost, dollars SHALL be unknown (null) and accepted tasks per dollar SHALL be `undefined`. Accepted tasks per dollar SHALL also be `undefined` when dollars is 0.

#### Scenario: One attempt has no cost
- **WHEN** a COMPLETE run has an attempt without `cost_usd`
- **THEN** dollars is null and accepted tasks per dollar is `undefined`

### Requirement: Shares SHALL be computed from raw attempt counts

Free, subscription, metered and unknown-capacity shares SHALL be fractions of model attempts (attempts with a route that is not `local/` and not mechanical), and aggregation across runs SHALL sum raw counts, never counts rebuilt from rounded shares.

#### Scenario: Share that does not round-trip through a float
- **WHEN** a run has 15 free attempts out of 22
- **THEN** the aggregate free share is exactly 15/22

### Requirement: The logged run id SHALL name the receipt

`build_run_receipt` SHALL take `run_id` from the digest-bound `run_started` event, falling back to `graph.json`'s `run_id` and then the run directory name only when no `run_started` event exists. A `graph.json` that carries a different `run_id` SHALL NOT make an untampered receipt fail verification, and editing any stored receipt field SHALL still fail verification.

#### Scenario: Held copy with a planner-input graph
- **WHEN** an untampered run's `graph.json` declares `run_id` `bod341-par3` while its `run_started` event and stored receipt say `20261118T031028Z`
- **THEN** `verify_run_receipt` returns no problems

#### Scenario: Edited receipt field
- **WHEN** a stored receipt's `run_id`, `goal` or `outcome` is edited
- **THEN** `verify_run_receipt` reports that field's mismatch
