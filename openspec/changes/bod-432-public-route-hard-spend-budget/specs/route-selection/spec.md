# Spec Delta

## Purpose

The public `/v1/route` endpoint validates and enforces a per-request hard spend budget in USD and returns an opt-in, per-request routing trace showing the admitted set, ordered shortlist, optimizer outcome, and rejection codes — without changing the legacy response shape or authority.

## MODIFIED Requirements

### Requirement: Read-only choose
The choose command with --no-confirm SHALL never send a model confirmation or write selection evidence. It SHALL select only routes with fresh exact-route proof and otherwise return no eligible route.

#### Scenario: No fresh proof
- **WHEN** --no-confirm is set and no route has fresh proof
- **THEN** selected is null and no confirmation is sent

*(Note: This requirement is listed as MODIFIED because the OpenSpec change adds a new route-selection capability that interacts with the public optimizer contract. The requirement text itself is unchanged.)*

## ADDED Requirements

### Requirement: Public route hard spend budget
The public `POST /v1/route` endpoint SHALL accept an optional `hard_spend_budget` field in the `execution_path_request` body. The value SHALL be a JSON number (integer or float) representing USD, finite, and strictly greater than zero. An absent field SHALL preserve existing behaviour (no budget enforcement). An explicit `null`, boolean, string, array, object, zero, negative, non-finite, or overflow value SHALL return HTTP 400 with an error message naming `execution_path_request.hard_spend_budget`. When a budget is provided, the execution-path qualifier SHALL reject offers with `exceeds_hard_spend_budget` when `expected_cost.cash_usd > budget`, and with `unknown_price_not_treated_as_free` when `expected_cost.has_unknown_cash` is true. When no budget is provided, unknown pricing SHALL remain advisory only (A25 invariant). Exact-budget equality (`cash_usd == budget`) SHALL qualify if the offer is otherwise eligible.

#### Scenario: Valid integer budget
- **WHEN** `execution_path_request.hard_spend_budget` is `5`
- **THEN** the request is accepted and the budget is enforced during qualification
- **TEST** `tests/test_public_route_budget.py::test_valid_integer_budget`

#### Scenario: Valid fractional budget
- **WHEN** `execution_path_request.hard_spend_budget` is `0.01`
- **THEN** the request is accepted and the budget is enforced during qualification
- **TEST** `tests/test_public_route_budget.py::test_valid_fractional_budget`

#### Scenario: Omitted budget preserves existing behaviour
- **WHEN** `execution_path_request` does not contain `hard_spend_budget`
- **THEN** the request is accepted and no budget enforcement occurs (unknown price remains advisory)
- **TEST** `tests/test_public_route_budget.py::test_omitted_budget`

#### Scenario: Explicit null is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `null`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_explicit_null_rejected`

#### Scenario: Boolean is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `true` or `false`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_boolean_rejected`

#### Scenario: String is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `"5"`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_string_rejected`

#### Scenario: Array is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `[]`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_array_rejected`

#### Scenario: Object is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `{}`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_object_rejected`

#### Scenario: Zero is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `0`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_zero_rejected`

#### Scenario: Negative is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `-1`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_negative_rejected`

#### Scenario: NaN is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `NaN`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_nan_rejected`

#### Scenario: Infinity is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `Infinity`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_infinity_rejected`

#### Scenario: Float overflow is rejected
- **WHEN** `execution_path_request.hard_spend_budget` is `1e400`
- **THEN** HTTP 400 with message containing `execution_path_request.hard_spend_budget`
- **TEST** `tests/test_public_route_budget.py::test_overflow_rejected`

#### Scenario: Above-budget offer is rejected with exceeds_hard_spend_budget
- **WHEN** a budget of `5` is set and an offer has `expected_cost.cash_usd = 6`
- **THEN** the offer is rejected with code `exceeds_hard_spend_budget` and cannot be selected
- **TEST** `tests/test_public_route_budget.py::test_above_budget_rejected`

#### Scenario: Unknown price under budget is rejected with unknown_price_not_treated_as_free
- **WHEN** a budget of `5` is set and an offer has `expected_cost.has_unknown_cash = true`
- **THEN** the offer is rejected with code `unknown_price_not_treated_as_free` and cannot be selected
- **TEST** `tests/test_public_route_budget.py::test_unknown_price_under_budget_rejected`

#### Scenario: Exact-budget offer qualifies if otherwise eligible
- **WHEN** a budget of `5` is set and an offer has `expected_cost.cash_usd = 5` and passes all other gates
- **THEN** the offer qualifies and can be selected
- **TEST** `tests/test_public_route_budget.py::test_exact_budget_qualifies`

#### Scenario: No budget leaves unknown price advisory (A25)
- **WHEN** no `hard_spend_budget` is provided and an offer has `expected_cost.has_unknown_cash = true`
- **THEN** the offer is not rejected on price grounds; unknown price remains advisory
- **TEST** `tests/test_public_route_budget.py::test_no_budget_unknown_price_advisory`

### Requirement: Public route routing trace
The public `POST /v1/route` endpoint SHALL accept an optional typed query parameter `include_routing_trace: bool = False`. When `true`, the response SHALL include a `routing_trace` object containing:
- `admitted_candidate_ids`: a set (JSON array) of candidate IDs admitted by the receipt, narrowed to the offered universe (or `confirmed.admitted` on the live confirmation path).
- `shortlist_candidate_ids`: an ordered array of candidate IDs from the final candidate pool shortlist (`ep_request.pool_receipt.shortlist` route_ids).
- `optimizer_decision`: an object with `outcome` (string, e.g., "selected", "denied") and `selected_candidate_id` (string or null) from the `ExecutionPathDecision`.
- `rejected_offers`: an array of objects each with `candidate_id` (string) and `code` (string, the rejection reason) from the `ExecutionPathDecision.rejected` list.

The trace SHALL be a read-only projection of real stage outputs from the same request, captured once in `IntelligenceService._prepare_execution_path_request_at` and carried via an internal `RoutingDecision.routing_trace` field. It SHALL be present on all authoritative outcomes, including no-selection responses (`denied`, `blocked`), when an authoritative decision was reached. It SHALL NOT fabricate unavailable stages (e.g., omit `shortlist_candidate_ids` if no pool receipt exists). The legacy response (without the query parameter) SHALL be unchanged. Authority is unchanged: a request without `execution_path_request` returns 400.

#### Scenario: Opt-in trace returns all four fields on selection
- **WHEN** `POST /v1/route?include_routing_trace=true` with a valid `execution_path_request` that results in a selection
- **THEN** the response includes `routing_trace` with `admitted_candidate_ids`, `shortlist_candidate_ids`, `optimizer_decision` (outcome "selected", non-null `selected_candidate_id`), and `rejected_offers`
- **TEST** `tests/test_public_route_routing_trace.py::test_trace_on_selection`

#### Scenario: Opt-in trace present on no-selection (denied)
- **WHEN** `POST /v1/route?include_routing_trace=true` with a valid `execution_path_request` that results in `denied` (no eligible route)
- **THEN** the response includes `routing_trace` with `admitted_candidate_ids`, `shortlist_candidate_ids`, `optimizer_decision` (outcome "denied", null `selected_candidate_id`), and `rejected_offers`
- **TEST** `tests/test_public_route_routing_trace.py::test_trace_on_denied`

#### Scenario: Opt-in trace present on no-selection (blocked)
- **WHEN** `POST /v1/route?include_routing_trace=true` with a valid `execution_path_request` that results in `blocked` (e.g., exhausted pool)
- **THEN** the response includes `routing_trace` with available fields; unavailable stages are omitted, not fabricated
- **TEST** `tests/test_public_route_routing_trace.py::test_trace_on_blocked`

#### Scenario: Legacy response unchanged without query param
- **WHEN** `POST /v1/route` (no `include_routing_trace`) with a valid `execution_path_request`
- **THEN** the response does not contain a `routing_trace` key
- **TEST** `tests/test_public_route_routing_trace.py::test_legacy_response_unchanged`

#### Scenario: Authority unchanged — missing execution_path_request is 400
- **WHEN** `POST /v1/route?include_routing_trace=true` without `execution_path_request` in body
- **THEN** HTTP 400 (no trace, authority check happens first)
- **TEST** `tests/test_public_route_routing_trace.py::test_authority_unchanged`

### Requirement: A25 invariance with budget and trace
When a hard spend budget is set and multiple eligible candidates exist, label-only and price-only mutations that preserve budget eligibility SHALL keep the admitted membership, ordered shortlist, optimizer outcome/choice, and public selection unchanged. Assertions SHALL use structured `routing_trace` fields, not reason prose.

#### Scenario: Label-only mutation preserves budget eligibility and selection
- **WHEN** a request with budget `B` selects route `R`; a label-only change (e.g., `is_free` flip that does not affect budget qualification) is applied to another candidate
- **THEN** `routing_trace.admitted_candidate_ids`, `shortlist_candidate_ids`, `optimizer_decision`, and public selection are identical
- **TEST** `tests/test_public_route_a25_invariance.py::test_label_only_mutation`

#### Scenario: Price-only mutation preserves budget eligibility and selection
- **WHEN** a request with budget `B` selects route `R`; a price-only change within the same budget tier is applied to another candidate
- **THEN** `routing_trace.admitted_candidate_ids`, `shortlist_candidate_ids`, `optimizer_decision`, and public selection are identical
- **TEST** `tests/test_public_route_a25_invariance.py::test_price_only_mutation`