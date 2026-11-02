# OmniRoute Advisory Rankings Integration into Verdict Selection

## Why
Verdict currently selects models through the EligibilityLadder using only local evidence (capacity tier, AA ratings, declared tier, quota, provider preference, load, fit). OmniRoute publishes free-provider rankings for coding, debugging, and review categories with provider-level scores, reliability aggregates, and usage metrics. This change integrates those rankings as optional advisory signals that reorder only already-eligible routes within their capacity tier, preserving hard admission authority and safe fallback behavior.

## What Changes
- Add optional OmniRoute rankings loader (thin adapter using existing bounded HTTP transport and credential resolver) that fetches `/api/free-provider-rankings?category=coding|debugging|review&configuredOnly=1&availableOnly=1&withUsage=1&usageRange=24h`
- Extend `EligibilityLadder` to accept optional `rankings: Mapping[str, int]` mapping exact `provider/model` route IDs to advisory rank (lower = better)
- Propagate task category (coding/debugging/review) from `verdict choose` action through `select_route_for_task` → `build_selector` → `EligibilityLadder` without overhauling `WorkNode`
- Modify `_rank_components` to include `advisory_rank` component (default `inf`) that sorts within capacity tier after rating, before slack/quota/provider_pref
- Preserve exact provider/model match only; no fuzzy prefix/version transfer; provider-best catalog model never implies model entitlement
- Keep Arena (0.4–0.98) and AA (0–100) metrics separate; never mix arithmetically
- Missing, malformed, stale (>12h), 404, timeout, generic-gateway, or unmatched rankings produce named omission (`rankings_unavailable`, `rankings_stale`, `rankings_malformed`, `rankings_gateway_unsupported`, `rankings_no_match`) and identical baseline order
- Receipts expose `category`, `source`, `observed_time`, `reliability/usage_context`, and `omission`; no fabricated `benchmark_as_of` or zero-filled unknown scores
- No new router, cache, database, telemetry subsystem, or probe loop

## Capabilities
### Modified Capabilities
- `095-evidence-based-model-chooser` — adds advisory rankings as optional tie-break within capacity tier for coding/debugging/review categories

### New Capabilities
- None (all behavior changes are deltas to existing capability)

## Impact
- **Affected code**: `verdict/orchestration/eligibility.py` (`EligibilityLadder.__init__`, `_rank_components`, `_rank_key`, `_probe_order`), `verdict/orchestration/eligibility_report.py` (`build_selector`), `verdict/orchestration/selection.py` (`select_route_for_task`), `verdict/actions/extra.py` (`_action_choose_live`), new optional `verdict/omniroute_rankings.py` adapter
- **Tests**: new focused tests in `tests/test_omniroute_rankings.py` (exact-match, mismatch, alias, metric-separation, excluded-top, fallback, rank-component/explain)
- **Receipts**: `choose` JSON gains `ranking.category`, `ranking.source`, `ranking.observed_time`, `ranking.reliability`, `ranking.usage`, `ranking.omission`
- **ADRs**: ADR-0001 (hard eligibility first; gateway evidence does not own strategy), ADR-020 (optional provider-neutral boundary; unknown is not permission; secrets stay out)

## Evidence Boundary
- **KNOWN**: OmniRoute API endpoint `/api/free-provider-rankings` with query params `category`, `configuredOnly`, `availableOnly`, `withUsage`, `usageRange`; response shape with `rankings[]` containing `provider`, `topModel.modelId`, `score`, `eloRaw`, `confidence`, `category`, `averageScore`, `modelCount`, `reliability.state`, `reliability.connections`, `reliability.usage{requests,successes,successRate,avgLatencyMs,lastRequestAt,windowHours}`; local gateway at `http://127.0.0.1:20128`; existing `OmniRouteHTTPTransport.bounded_get_json` and `resolve_gateway_binding`; existing `EligibilityLadder._rank_components`, `_rank_key`, `_probe_order`; existing `build_selector`, `select_route_for_task`, `_action_choose_live`; existing receipt structure via `explain_selection`
- **INFERRED**: Category mapping from `--task-class implementation/debugging/review` to API categories; exact route_id matching via existing catalog alias mapping; 12h staleness threshold from existing metadata freshness policy; 5s timeout from existing bounded transport defaults
- **NOT AVAILABLE**: API does not expose `benchmark_as_of`; no per-model ranking for all catalog models (only provider-best); no circuit breaker or per-model lockout in `availableOnly`; Arena and AA metrics not comparable

## Acceptance Criteria
1. **AC1** - Coding, debugging, review categories flow from verdict choose into shared selector; optional OmniRoute rankings act only after admission. Verified by: tracked red/green tests plus parent real choose commands for all three categories.
2. **AC2** - Only exact provider/model or an existing trusted canonical alias receives a ranking; provider-best evidence never transfers and Arena/AA metrics remain separate. Verified by: exact-match, mismatch, alias, and metric-separation unit tests.
3. **AC3** - Authentication, entitlement, capability, quota, capacity, availability, authority, and risk gates remain authoritative; an excluded top-ranked route cannot win. Verified by: excluded-top regression plus admission and selection neighbor suite.
4. **AC4** - Missing, malformed, stale, 404, timeout, generic-gateway, and unmatched ranking data produce a named omission and identical baseline order. Verified by: fallback tests compare order and receipt reasons to no-rankings baseline.
5. **AC5** - Existing receipts expose category, source, observed time, reliability/usage context, and omission without fabricated benchmark time or zero-filled unknown scores. Verified by: rank-component/explain tests and parent real JSON proof.
6. **AC6** - Accepted ADRs govern the design; an OpenSpec delta maps these scenarios and validates strictly; parent OCR reviews the exact integrated head and fails closed. Verified by: strict OpenSpec, deterministic gates, and exact-head OpenCodeReviewer artifact.
