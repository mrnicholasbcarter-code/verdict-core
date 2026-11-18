# Design

## Context

The public `POST /v1/route` endpoint accepts an `execution_path_request` body that describes candidate offers with pricing (`PriceEvidenceInput`), free flags, and capability evidence. The internal `ExecutionPathRequest` dataclass already has a `hard_spend_budget: float | None` field, and `_qualify_offer` (verdict/execution_path.py:348) enforces it:
- When `hard_spend_budget is not None` and `offer.expected_cost.has_unknown_cash` → `unknown_price_not_treated_as_free`
- When `hard_spend_budget is not None` and `offer.expected_cost.cash_usd > Decimal(str(hard_spend_budget))` → `exceeds_hard_spend_budget`
- When `hard_spend_budget is None`, unknown price is advisory only (A25 invariant).

However, `_public_execution_path_request` (verdict/api.py:1119) does not read or validate a `hard_spend_budget` from the public JSON, so the qualifier never sees it. Operators cannot bound spend on a public optimizer call.

Additionally, there is no structured, per-request diagnostic trace. The `route_explain` endpoint exists for cache/eligibility inspection, but a per-request trace of the actual admission→shortlist→optimizer decision is only available by correlating a subsequent explain lookup with the request ID. Operators need an opt-in, same-response trace showing admitted set, ordered shortlist, optimizer outcome/choice, and rejection codes.

## Goals / Non-Goals

**Goals:**
- Validate `hard_spend_budget` at the public API boundary: numeric USD only, finite, `> 0`; absent = unchanged; explicit `null`/boolean/string/array/object/zero/negative/non-finite/overflow = 400 naming `execution_path_request.hard_spend_budget`.
- Thread the validated value to `ExecutionPathRequest.hard_spend_budget` so `_qualify_offer` enforces it.
- Add `include_routing_trace: bool = False` as a plain typed query parameter on `POST /v1/route`.
- When opted in, return a `routing_trace` object with `admitted_candidate_ids` (set), `shortlist_candidate_ids` (ordered), `optimizer_decision` (outcome + selected_candidate_id or null), `rejected_offers` (candidate_id + code).
- Trace is a read-only projection of real stage outputs from the same request: admitted IDs from `receipt.admitted` narrowed to offered IDs (or `confirmed.admitted` on live path), shortlist from `ep_request.pool_receipt.shortlist` route_ids, optimizer decision from the `ExecutionPathDecision`. Never fabricate unavailable stages.
- Legacy response (without query param) and authority (400 without `execution_path_request`) unchanged.

**Non-Goals:**
- Change the optimizer, admission ladder, or qualification logic.
- Change any gate, dependency, or install behaviour.
- Expose evidence payloads, secrets, or internal scoring in the trace.
- Add a new endpoint; the trace is an opt-in field on the existing response.

## Decisions

### `hard_spend_budget` validation

- Accept `int` or `float` (JSON number). Reject `bool` (since `isinstance(True, int)` is true in Python, check `isinstance(v, bool)` first).
- Finite check: `math.isfinite(v)`.
- Positive check: `v > 0`.
- Absent key: do not set `ExecutionPathRequest.hard_spend_budget` (leaves it `None`).
- Explicit `null` or invalid type/value: raise `ExecutionPathError` with message containing `execution_path_request.hard_spend_budget`.
- Validation happens in `_public_execution_path_request` before any admission/qualification work.

### `include_routing_trace` query parameter

- Add `include_routing_trace: bool = False` to the `route_task` signature (FastAPI will treat it as a typed query parameter, matching `route_explain` at lines 1806-1813).
- Do not parse `request.query_params` manually.
- When `True`, include `routing_trace` in the JSON response.

### Routing trace structure

```json
{
  "routing_trace": {
    "admitted_candidate_ids": ["route-id-1", "route-id-2"],
    "shortlist_candidate_ids": ["route-id-1", "route-id-2"],
    "optimizer_decision": {
      "outcome": "selected",
      "selected_candidate_id": "route-id-1"
    },
    "rejected_offers": [
      { "candidate_id": "route-id-3", "code": "exceeds_hard_spend_budget" }
    ]
  }
}
```

- `admitted_candidate_ids`: set (JSON array, order not significant) of IDs from `receipt.admitted` ∩ offered_ids, or `confirmed.admitted` on the live confirmation path.
- `shortlist_candidate_ids`: ordered list of `route_id` from `ep_request.pool_receipt.shortlist`.
- `optimizer_decision.outcome`: the `ExecutionPathDecision`'s effective outcome (e.g., "selected", "denied", "blocked"). `selected_candidate_id`: the decision's `selected_candidate_id` (string or null).
- `rejected_offers`: array of objects from the decision's `rejected` list, each with `candidate_id` and `code` (the `reason` field).
- Present on all authoritative outcomes including no-selection (`denied`, `blocked`); if a stage was not reached (e.g., no pool receipt), omit that field rather than fabricate.

### Trace plumbing

- `IntelligenceService._prepare_execution_path_request_at` (lines 1371-1489) already builds the receipt, pool, and confirmation. At the end, it has:
  - `offered_admitted` (line 1372): admitted IDs narrowed to offered universe
  - `pool.shortlist` (line 1400): ordered shortlist
  - The final `ExecutionPathDecision` returned by `optimize_execution_path`
- Snapshot these once per request and attach to the `RoutingDecision` returned by `intelligence.route()`.
- `RoutingDecision` (verdict/models.py:35) gains an optional internal field `routing_trace: dict | None = None` (not serialized by `_safe_decision_dict` unless explicitly projected).
- `route_task` projects `decision.routing_trace` into the response when `include_routing_trace=True`.

### Authority and legacy behaviour

- No `execution_path_request` in body → 400 (existing behaviour, unchanged).
- Without `?include_routing_trace=true` → response unchanged (no `routing_trace` key).
- With `?include_routing_trace=true` but no authoritative decision (e.g., early 400) → no trace (the 400 response is not a routing decision).

## Risks / Trade-offs

- Cross-layer trace plumbing is high risk: `route_task` receives only `RoutingDecision`, which has no admitted set today. Once `_prepare_execution_path_request_at` folds admission into the prepared request, reconstructing it in the API can misreport the set. **Mitigation:** snapshot at the source (in `_prepare_execution_path_request_at`) and test that the same decision carries the trace to the API without leaking it into the legacy response.
- Public input/output contracts can regress legacy clients. **Mitigation:** opt-in diagnostics, test unchanged legacy shape/status.
- Stage diagnostics can misrepresent authority. **Mitigation:** capture once from the same request, never infer from winner or prose.

## Security / trust implications

- The trace exposes candidate IDs and rejection codes, not evidence payloads, pricing details, or secrets.
- `hard_spend_budget` is a caller filter (like `spend_policy`); it does not change admission authority.
- No new attack surface; validation is strict and fails closed.

## Routing / context / memory implications

- No routing policy change. The trace is a read-only projection.
- No context pipeline or memory store change.

## Migration / rollback

- No data migration.
- Reverting removes the validation and trace projection; legacy behaviour restored.

## ADR impact

- ADR 0001 (control plane invariants): admission narrows only; budget is a narrowing filter.
- ADR-036 (goal-to-receipt orchestration): trace is a receipt augmentation, not a new authority.
- No ADR file changes required.