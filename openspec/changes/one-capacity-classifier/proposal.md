# ONE capacity classifier (S1)

**Ticket:** BOD-342 (model-discovery S1)
**Branch:** feat/one-capacity-classifier

## Problem

Capacity decisions (FREE / METERED / SUBSCRIPTION / UNKNOWN) were made in at
least four places, and two of them used the route name (`:free` suffix) as
evidence of free capacity. Names are not facts. This created routes that were
admitted as free based on a naming convention rather than a declared gateway
fact, violating the operator rule introduced 2026-11-19.

Additionally `gateway_free` in `intelligence.py` was built as
`f"{row.provider}/{row.model_id}"` strings, which double-prefixed entries
whose `model_id` already carried the provider, e.g.
`kc/kc/meta-llama/model`. This meant real free-tier rows failed the
`route_id in gateway_free` membership test silently.

## Change

### Single public entry point

`verdict/capacity.py` gains `capacity_of(conn, row, *, route_id,
openrouter_free_set) -> (CapacityClass, deciding_fact)` as the one place all
production code must call for a capacity decision. The existing
`classify_route_capacity` (3-tuple form) is kept for callers that also need
the account plan label.

`verdict/intelligence.py` is updated to call `capacity_of` instead of
`classify_route_capacity(...)[0]`.

### Name-based evidence removed

`verdict/free_route_harvest.py`:
- `free_status()` no longer returns `"free"` for a `:free` suffix. Only an
  explicit `free_ids` membership decides.
- `pick_best_live()` no longer treats `:free` as evidence. Without `free_ids`
  no identity qualifies.

`verdict/omniroute_catalog.py`:
- `rank()` no longer includes a name-derived free rank. Sort is by provider
  then model_id only.

`verdict/autodev_run.py`:
- Bootstrap route check drops the `:free` suffix requirement.

`verdict/free_tier_admit.py`:
- `FreeTierModel` gains a `route_id` property that normalises the
  provider-prefix double-encoding and lower-cases the provider.
- `_is_positively_free_identity` (dead code, name-heuristic) removed.
- `_looks_free_by_name` (name-heuristic) removed.
- `_choose_sort` and `admit_free_tier_active` updated to use `row.route_id`.

### gateway_free format fix

`intelligence.py` builds `gateway_free` as `{row.route_id for row in
snapshot.free_tier}` so the set members always match the route IDs used as
lookup keys.

### Forbidden-symbol guards

`reachability-forbidden.json` gains two entries:
- `verdict.omniroute_pipeline.free_rule.classify_free` — allowed only in
  `verdict.capacity` and `verdict.omniroute_pipeline.census_hook`.
- `verdict.orchestration.provider_catalog.connection_signals_free` — allowed
  only in `verdict.capacity`.

These prevent a second capacity implementation from silently growing back.

## Invariants

- A `:free` suffix or any other name pattern is NOT evidence of free capacity.
- `capacity_of` is the single call site for all capacity decisions.
- `FreeTierModel.route_id` never double-prefixes.
