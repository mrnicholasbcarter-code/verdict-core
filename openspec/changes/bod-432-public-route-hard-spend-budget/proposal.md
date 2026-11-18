# Proposal: Public /v1/route honours hard_spend_budget and returns structured routing trace

## Why

The public `/v1/route` endpoint with an `execution_path_request` body currently accepts candidate offers with pricing but does not enforce a per-request hard spend ceiling. The `ExecutionPathRequest` type already carries a `hard_spend_budget: float | None` field, and `_qualify_offer` rejects offers that exceed it or have unknown cash when a budget is set. However, the public API boundary in `_public_execution_path_request` does not read or validate a `hard_spend_budget` from the request, so the qualifier never sees it. Operators cannot bound spend on a public optimizer call, and there is no structured, per-request diagnostic trace showing which candidates were admitted, shortlisted, chosen, or rejected and why.

## What Changes

- Add `hard_spend_budget` to the public `execution_path_request` contract: numeric USD only, finite and `> 0`; absent preserves existing behaviour; explicit `null`, boolean, string, array, object, zero, negative, non-finite, or float overflow returns **400** naming `execution_path_request.hard_spend_budget`.
- Wire the validated value through to `ExecutionPathRequest.hard_spend_budget` so `_qualify_offer` enforces it:
  - Exact budget: offer with `cash_usd == budget` qualifies if otherwise eligible.
  - Above budget: offer rejected with `exceeds_hard_spend_budget`.
  - Unknown price under a budget: offer rejected with `unknown_price_not_treated_as_free`.
  - No budget: unknown price remains advisory (A25 invariant).
- Add `include_routing_trace: bool = False` as a plain typed query parameter on `POST /v1/route` (matching `route_explain`). When `true`, the response includes a `routing_trace` object with:
  - `admitted_candidate_ids`: set of admitted candidate IDs (from receipt.admitted narrowed to offered IDs, or confirmed.admitted on the live path)
  - `shortlist_candidate_ids`: ordered shortlist route_ids from the final `ep_request.pool_receipt.shortlist`
  - `optimizer_decision`: `{ outcome, selected_candidate_id }` from the same `ExecutionPathDecision`
  - `rejected_offers`: array of `{ candidate_id, code }` from the same decision's `rejected` list
  - Present on all authoritative outcomes, including no-selection responses; never fabricate unavailable stages.
- Legacy response shape (without `?include_routing_trace=true`) is unchanged.
- Authority is unchanged: a request without `execution_path_request` still returns 400.

## Capabilities

### New Capabilities

- `public-route-hard-spend-budget`: The public `/v1/route` endpoint validates and enforces a per-request hard spend budget in USD. Exact-match offers qualify; above-budget and unknown-cash-under-budget offers are rejected with structured codes.
- `public-route-routing-trace`: The public `/v1/route` endpoint returns an opt-in, per-request routing trace (`admitted_candidate_ids`, `shortlist_candidate_ids`, `optimizer_decision`, `rejected_offers`) without changing the legacy response or relying on a subsequent explain lookup.

### Modified Capabilities

- `execution-path-request-contract`: The public `execution_path_request` object now accepts an optional `hard_spend_budget` field with strict validation.

## Impact

- `verdict/api.py`: `_public_execution_path_request` validates `hard_spend_budget` and threads it into the `ExecutionPathRequest`; `route_task` adds the `include_routing_trace` query parameter and projects the internal `RoutingDecision.routing_trace` when opted in.
- `verdict/models.py`: `RoutingDecision` gains an optional internal `routing_trace` field (not serialized to client-facing decision dicts by default).
- `verdict/intelligence.py`: `_prepare_execution_path_request_at` snapshots admitted IDs, shortlist, and the `ExecutionPathDecision` once per request and attaches the trace to the returned `RoutingDecision`.
- `verdict/execution_path.py`: No logic change; the existing `hard_spend_budget` qualifier path is now exercised from the public API.
- Tests: new public HTTP tests for validation, budget enforcement, trace structure, and A25 invariance.

## Evidence boundary

KNOWN: `ExecutionPathRequest.hard_spend_budget` exists and `_qualify_offer` implements the rejection codes. KNOWN: `route_task` is the public entry and `_public_execution_path_request` builds the trusted request. NOT KNOWN until implemented: the public validation and trace projection work end-to-end; that is the wave-2 task.

## Acceptance criteria

- Strict validation of `bod-432-public-route-hard-spend-budget` passes; post-merge proof tasks have the required marker.
- `hard_spend_budget` accepts only numeric USD, finite, `> 0`; absent preserves old behaviour; explicit `null` and every other type is 400 naming `execution_path_request.hard_spend_budget`.
- `routing_trace` fields: `admitted_candidate_ids` (set), `shortlist_candidate_ids` (ordered), `optimizer_decision` (outcome and selected candidate ID or null), `rejected_offers` (candidate ID and code). Included on existing no-selection responses when an authoritative decision was reached; never fabricate unavailable stages.
- Provenance documented: `IntelligenceService._prepare_execution_path_request_at` (verdict/intelligence.py:1371-1489) captures admitted IDs from `receipt.admitted` narrowed to offered IDs or `confirmed.admitted` on the live path; ordered shortlist from `ep_request.pool_receipt.shortlist` route_ids. Complete the read-only trace once per request from those snapshots and the same `ExecutionPathDecision`. Carry it in an internal `RoutingDecision.routing_trace` field (verdict/models.py:35-77) to `route_task` (verdict/api.py:1397-1440).
- `include_routing_trace: bool = False` is a plain typed query parameter like `route_explain` (verdict/api.py:1806-1813), not `request.query_params`.
- Exact-budget eligibility, above-budget and unknown-price rejections, and unchanged authority/legacy response behaviour are documented.
- Post-merge live A25 checks written with the required marker.