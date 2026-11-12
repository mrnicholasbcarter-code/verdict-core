# Spec Delta

## Purpose

Defines server-owned task authority and price-invariant route selection at public entrypoints.

## ADDED Requirements

### Requirement: Public policy cannot be weakened by the client

The system SHALL reject reserved weakening flags in public route requests with `ExecutionPathError` before selection or send. Server-critical tasks SHALL keep `protected=True` regardless of client-supplied false protected or certificate fields. Offline catalog mode SHALL NOT authorize execution. The relay SHALL NOT forward reserved decision fields, including JSON null values, or typed requirement keys upstream. `RouteRequest` SHALL accept `task_family`, `min_context_tokens` (positive when present) and `reasoning_required`, and carry them into the execution-path request.

#### Scenario: Reserved flags and offline mode are refused
- **WHEN** a public request carries a reserved weakening flag, a false protected field, or offline catalog mode
- **THEN** the request is rejected or stays protected before any selector or upstream send
- Proof: `tests/test_serve_path_cutover.py::test_public_policy_cannot_be_weakened_and_optimizer_is_price_invariant`

#### Scenario: Relay strips reserved fields
- **WHEN** the relay receives `raw_decision: true`
- **THEN** it returns 400 with zero upstream sends (`tests/test_proxy.py::test_relay_rejects_raw_decision_flag_before_any_upstream_send`)
- **WHEN** reserved decision fields are JSON null
- **THEN** they do not reach the upstream body (`tests/test_proxy.py::test_relay_never_forwards_null_reserved_decision_fields_upstream`)

### Requirement: Selection is price-invariant and requires a trusted shortlist

The system SHALL order qualified offers by trusted `pool_receipt` shortlist position, then candidate id, independent of expected price. Missing price SHALL remain unknown and SHALL gate an offer only under an explicit `hard_spend_budget`. The system SHALL block with `missing_trusted_live_ranking` when no trusted shortlist ranking exists, and SHALL reject offers absent from a non-empty shortlist as `not_on_trusted_shortlist`.

#### Scenario: No receipt never falls back to price
- **WHEN** the optimizer runs without a `pool_receipt`
- **THEN** the decision is `blocked` with `missing_trusted_live_ranking`
- Proof: `tests/test_execution_path.py::test_missing_pool_receipt_fails_closed_never_price_ranking`

#### Scenario: Unknown price gates only under a hard budget
- **WHEN** an offer has unknown cash price and the request sets a hard spend budget
- **THEN** the offer is rejected as `unknown_price_not_treated_as_free` (`tests/test_execution_path.py::test_unknown_price_not_treated_as_free`)
- **WHEN** no hard spend budget is set
- **THEN** the offer stays selectable (`tests/test_execution_path.py::test_unknown_price_selectable_without_hard_budget_per_a25`)

#### Scenario: Off-shortlist offers are refused
- **WHEN** no offer is on the shortlist
- **THEN** the decision is blocked (`tests/test_execution_path.py::test_offers_not_on_trusted_shortlist_block_never_ordered_by_candidate_id`)
- **WHEN** a shortlisted offer exists
- **THEN** it wins and off-shortlist offers are rejected (`tests/test_execution_path.py::test_offer_off_the_shortlist_is_rejected_while_a_shortlisted_offer_wins`)

### Requirement: Live probe receipts are fresh and observed

`build_live_probe_receipt` SHALL require `now`, reject probe times in the future or older than `LIVE_PROBE_MAX_AGE_SECONDS`, require `x-omniroute-model` in probe headers, keep server rank order, shortlist only `eligible is True` candidates, and raise `CandidatePoolError` when none passed. Autodev selection SHALL name a refusal as `AutodevError`.

#### Scenario: Stale, future or unheadered probes are refused
- **WHEN** a probe is older than the bound, in the future, or lacks the served-model header
- **THEN** receipt construction raises `CandidatePoolError`
- Proof: `tests/test_candidate_pool.py::test_live_probe_receipt_rejects_future_and_stale_probes`, `test_live_probe_receipt_requires_the_served_model_header`, `test_live_probe_receipt_accepts_probe_exactly_at_the_age_bound`, `test_live_probe_receipt_keeps_server_rank_order_for_fresh_probes`

#### Scenario: Unpassed candidates are never shortlisted
- **WHEN** a candidate is not eligible, or none passed
- **THEN** it is not recorded as passed, and no passed candidate raises
- Proof: `tests/test_candidate_pool.py::test_live_probe_receipt_never_records_unpassed_candidates_as_passed`, `test_live_probe_receipt_without_a_passed_candidate_raises`

#### Scenario: Autodev names the refusal
- **WHEN** retry or live preparation meets a refused receipt
- **THEN** `AutodevError` names the refusal
- Proof: `tests/test_autodev_wiring.py::test_retry_selection_refuses_stale_probe_evidence`, `test_prepare_live_autodev_names_receipt_refusal_as_autodev_error`, `test_prepare_live_autodev_selected_names_receipt_refusal_as_autodev_error`
