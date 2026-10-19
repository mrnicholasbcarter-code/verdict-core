# Spec Delta

## Purpose

Defines bounded gateway inventory discovery that fails closed and keeps missing telemetry unknown.

## ADDED Requirements

### Requirement: Gateway inventory discovery is bounded and fails closed

`load_omniroute_admit_snapshot` SHALL enforce a per-HTTP timeout of 20 s, an aggregate deadline of 30 s, a cumulative decoded budget of 16 MiB, 50,000 raw rows and 100 pages, each as a parameter with that default. It SHALL follow only the adapter-supported `after` id cursor. Repeated cursors, a followed page with no new row ids, and unsupported pagination hints (`nextPage*`, truthy `hasMore`/`has_more`) SHALL raise `LiveAdmitError("incomplete_source", ...)`. A violated bound SHALL raise before the catalog is cached or a snapshot is built, so a truncated catalog is never published as complete. A last-good copy inside its original `fetched_at` deadline MAY mask `incomplete_source`.

#### Scenario: Bound violations are incomplete sources
- **WHEN** the catalog exceeds the row limit, page limit or decoded byte budget, hits an unsupported pagination hint, or returns an inconsistent page
- **THEN** the loader raises `incomplete_source`
- Proof: `tests/test_free_tier_admit.py::test_catalog_row_overflow_is_incomplete_source`, `test_catalog_page_overflow_is_incomplete_source`, `test_catalog_decoded_byte_budget_is_incomplete_source`, `test_unsupported_pagination_hint_is_incomplete_source`, `test_catalog_inconsistent_page_is_incomplete_source`

#### Scenario: Aggregate deadline
- **WHEN** the aggregate deadline is exhausted and no last-good copy exists
- **THEN** the loader fails closed (`test_aggregate_deadline_zero_without_last_good_fails_closed`)
- **WHEN** a last-good copy is inside its original deadline
- **THEN** it is served (`test_aggregate_deadline_zero_serves_original_deadline_last_good`)

#### Scenario: Per-request timeouts
- **WHEN** the catalog is read
- **THEN** the per-request timeout is 20 s and requested timeouts never exceed the aggregate deadline
- Proof: `tests/test_admit_snapshot_resilience.py::test_catalog_read_uses_the_bounded_per_request_timeout`, `test_requested_timeouts_never_exceed_the_aggregate_deadline`

### Requirement: Missing telemetry stays unknown

The snapshot SHALL record `fetched_at` (source time of the live read), `inventory_complete`, `inventory_diagnostics` (per-surface `shape_unknown` drops) and `unsupported_fields`. An unrecognised catalog shape SHALL be unknown, not zero. A valid empty `data: []` SHALL be an observed zero. Absent health telemetry SHALL NOT become healthy.

#### Scenario: Unknown versus observed zero
- **WHEN** the catalog has an unrecognised nested shape
- **THEN** `inventory_complete` is false with a diagnostic (`tests/test_free_tier_admit.py::test_unknown_catalog_shape_is_unknown_not_observed_zero`)
- **WHEN** telemetry is missing
- **THEN** it stays unknown and the catalog stays bounded (`tests/test_free_tier_admit.py::test_gateway_missing_telemetry_is_unknown_and_catalog_is_bounded`)
