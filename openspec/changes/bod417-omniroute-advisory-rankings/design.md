# Design: OmniRoute Advisory Rankings Integration

## Context
Verdict's model selection flows through `EligibilityLadder` which ranks candidates by: capacity tier → rating (AA task score → intelligence fallback → declared tier → unknown) → slack → quota_rank → provider_pref → load → -fit → route_id. OmniRoute publishes free-provider rankings for coding/debugging/review categories with provider-level scores (Arena 0.4–0.98), reliability aggregates, and usage metrics. This design integrates those rankings as an optional advisory signal that reorders only already-eligible routes within their capacity tier.

See proposal.md - Why for motivation.

## Goals / Non-Goals
### Goals
- Optional advisory rankings from OmniRoute affect ordering only within capacity tier, after hard admission
- Exact provider/model match only; no score transfer across models or providers
- Safe fallback: any rankings failure → named omission + identical baseline order
- Receipts expose category, source, observed_time, reliability/usage_context, omission
- No new router, cache, database, telemetry, or probe subsystem

### Non-Goals
- No new ranking catalog or override of admission from ELO scores
- No provider-wide "free" entitlement from provider-best catalog model
- No mixing of Arena and AA metrics
- No new TTL, constants, or cadence
- No WorkNode overhaul for category plumbing

## Decisions

### 1. Rankings Loader Adapter (New Module: `verdict/omniroute_rankings.py`)
**Why**: Separate concerns; reuse existing bounded HTTP transport (`OmniRouteHTTPTransport.bounded_get_json`) and credential resolver (`resolve_gateway_binding`); no hard dependency on OmniRoute for non-OmniRoute gateways.

**Alternative considered**: Inline fetch in `build_selector` — rejected (couples selection to network, harder to test, violates ADR-020 optional boundary).

**Implementation**:
- Function `fetch_rankings(gateway: str, category: str) -> Mapping[str, int] | None` returns route_id → advisory_rank (0 = best) or None on any failure
- Validates response: expects `rankings[]` array with `provider`, `topModel.modelId`, `score`, `reliability`, `usage`; computes rank by score desc within category
- Maps to exact `provider/model` route IDs using existing catalog alias mapping only
- 5s timeout, no retries; any exception → None (fallback handled upstream)
- Observed timestamp captured from response headers or local clock (not `benchmark_as_of`)

### 2. Category Propagation
**Why**: `verdict choose --task-class implementation/debugging/review` must drive which rankings category is fetched.

**Path**: `extra.py:_action_choose_live` (line 295: `family = profile_task(...).task_family`) → `selection.py:select_route_for_task` (new `category` param) → `eligibility_report.py:build_selector` (new `rankings_category` param) → `EligibilityLadder.__init__` (stores `self._rankings_category`)

**Default**: `None` (no rankings fetched) for backward compatibility with existing callers (orchestrate, autodev, etc.)

### 3. Advisory Rank Component in `_rank_components`
**Why**: Single source of truth for ranking values; `_rank_key` builds sort tuple from it.

**Change**: Add `advisory_rank: int = inf` to returned dict. In `_rank_key`, insert at position 5 (after `declared_tier`, before `slack`):
```python
c["advisory_rank"]  # smaller wins; inf = no ranking
```

**Logic**: Only populated when `self._rankings_category` matches the candidate's task family AND exact `route_id` exists in `self._rankings` mapping. `inf` default ensures no-ranking routes sort after ranked ones within same tier.

### 4. `_probe_order` Consistency
**Why**: Probe order must match final ranking order so confirmation scheduling doesn't pick a worse-known rank.

**Change**: In `_probe_order` sort key, add advisory_rank after rating:
```python
*(_CAPACITY_ORDER.get(a.capacity, 3), *(rating_key(a.rating) if a.rating else (3,0,0)), getattr(a, "advisory_rank", float("inf")))
```

### 5. Receipt / Explain Updates
**Why**: AC5 requires transparency in receipts.

**Change**: `explain_selection` and `_rank_components_for_display` include:
- `ranking.category` (str | null)
- `ranking.source` ("omniroute" | null)
- `ranking.observed_time` (ISO string | null)
- `ranking.reliability` (dict | null) — provider aggregate from API
- `ranking.usage` (dict | null) — 24h requests/successes/rate/latency
- `ranking.omission` (str | null) — one of: `rankings_unavailable`, `rankings_stale`, `rankings_malformed`, `rankings_gateway_unsupported`, `rankings_no_match`, or null

Unknown scores → `null` not `0`. No `benchmark_as_of` field.

### 6. Fallback Behavior
**Why**: AC4 requires identical baseline order on any rankings failure.

**Implementation**: In `build_selector`, wrap rankings fetch in try/except; on any exception or None result, set `omission` and pass `rankings=None` to `EligibilityLadder`. Ladder treats `None` as "no rankings available" → all `advisory_rank = inf` → baseline order preserved.

## Security / trust implications
- No new credentials or secret handling: reuses existing `resolve_gateway_binding()` and `OmniRouteHTTPTransport` which already manage the OmniRoute API key via `VERDICT_OMNIROUTE_API_KEY` env var
- No secret logging: adapter does not log response bodies containing scores or usage data
- No credential pooling or rotation changes: uses existing resolver only
- Advisory rankings are unauthenticated data from gateway; they never bypass admission gates (ADR-0001) and are treated as untrusted input that only affects tie-breaking within already-eligible routes
- Generic OpenAI gateways return `omission="rankings_gateway_unsupported"` — no attempt to discover or call rankings endpoint on non-OmniRoute origins
- Controller environment never receives keys; keys stay in existing resolver

## Routing / context / memory implications
- No new routing layer: advisory rankings integrate into existing `EligibilityLadder` ranking pipeline, not a separate router
- No new cache or database: rankings fetched on-demand per selection; no persistent storage; staleness handled by omission not caching
- No new telemetry/probe subsystem: uses existing bounded HTTP transport; no background polling or census
- Context propagation: task category flows from `choose` action through `select_route_for_task` → `build_selector` → `EligibilityLadder` via new parameters; no `WorkNode` changes
- Memory: no new session memory entries; rankings not cached across selections; receipt includes `observed_time` for auditability
- No changes to `verdict/orchestration/run.py`, `verdict/orchestration/verified_refresh.py`, or any heartbeat/refresh machinery

## ADR impact
- **ADR-0001 (Verdict Control Plane Invariants)**: Hard eligibility first; gateway evidence does not own strategy. This design preserves admission authority — rankings only reorder within capacity tier after all gates pass. Explicitly does not add new admission gates or override existing ones.
- **ADR-020 (Gateway Adapter Contracts)**: Optional provider-neutral boundary; unknown is not permission; secrets stay out. The adapter is optional (returns None on non-OmniRoute gateways), does not invent permissions from unknown scores, and keeps secrets in existing resolver.
- **ADR-002 (Routing)**: Not modified — no new routing paths or strategies added.
- **ADR-035 (Execution Path Authority)**: Not modified — selection authority remains in `EligibilityLadder`; rankings are advisory input only.

## Risks / Trade-offs
| Risk | Mitigation |
|------|------------|
| Rankings API shape changes | Adapter validates response schema; any validation error → None → fallback |
| Staleness threshold arbitrary | Reuse existing 12h metadata freshness policy; no new TTL constant |
| Category inference ambiguity | Only explicit `--task-class` + `--requires` drive category; no heuristic |
| Generic OpenAI gateway | Adapter returns None for non-OmniRoute gateways (detected via URL pattern); omission = `rankings_gateway_unsupported` |
| Provider-best ≠ model entitlement | Exact route_id match only; provider-best catalog model never transfers |

## Migration Plan
1. SPEC phase complete (this change directory)
2. Implementation phase (separate packet):
   - Create `verdict/omniroute_rankings.py` adapter
   - Modify `EligibilityLadder.__init__`, `_rank_components`, `_rank_key`, `_probe_order`
   - Modify `build_selector`, `select_route_for_task`, `_action_choose_live` for category propagation
   - Update `explain_selection` and `_rank_components_for_display`
   - Add focused tests in `tests/test_omniroute_rankings.py`
3. Local validation: RED→GREEN, ruff/mypy, `openspec validate --strict`
4. Parent OCR review on exact integrated head (fail-closed)
5. Parent re-runs real CLI proof from installed merged main

## Open Questions
None — all resolved in hydration pack and operator comments.
