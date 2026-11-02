# Spec Delta: evidence-based-model-chooser

## ADDED Requirements

### Requirement: Advisory rankings reorder eligible routes within capacity tier
When OmniRoute rankings are available for the task category (coding, debugging, review), the EligibilityLadder SHALL apply an advisory rank component that reorders already-eligible routes within their capacity tier. The advisory rank SHALL sort after rating (task score, intelligence fallback, declared tier) and before slack, quota_rank, provider_pref, load, and fit.

#### Scenario: Coding category rankings reorder free tier
- **WHEN** task category is "coding" and OmniRoute returns rankings for coding with route `nvidia/meta/llama-3.2-11b-vision-instruct` at rank 0
- **THEN** that route receives `advisory_rank=0` and sorts before other free-tier routes with same rating

#### Scenario: Debugging category uses separate rankings
- **WHEN** task category is "debugging" and OmniRoute returns rankings for debugging
- **THEN** only debugging-category rankings are used; coding rankings are ignored

#### Scenario: Review category uses separate rankings
- **WHEN** task category is "review" and OmniRoute returns rankings for review
- **THEN** only review-category rankings are used; other categories are ignored

#### Scenario: Non-matching category ignores rankings
- **WHEN** task category is "architecture" (no rankings category)
- **THEN** no rankings are fetched; all routes receive `advisory_rank=inf`

### Requirement: Exact provider/model match only
Rankings SHALL apply only to exact `provider/model` route IDs present in the admitted pool. Provider-best catalog model SHALL NOT transfer score to other models from the same provider. No fuzzy prefix/version matching.

#### Scenario: Exact match receives ranking
- **WHEN** ranking exists for `nvidia/meta/llama-3.2-11b-vision-instruct` and that exact route is admitted
- **THEN** that route receives the advisory rank

#### Scenario: Provider sibling without ranking unchanged
- **WHEN** ranking exists for `nvidia/meta/llama-3.2-11b-vision-instruct` but admitted route is `nvidia/other-model`
- **THEN** `nvidia/other-model` receives `advisory_rank=inf` (no transfer)

#### Scenario: Canonical alias receives ranking
- **WHEN** ranking exists for `provider/model` and admitted route is a trusted canonical alias mapping to same `provider/model`
- **THEN** the alias route receives the advisory rank

#### Scenario: No fuzzy version transfer
- **WHEN** ranking exists for `provider/model-v1` and admitted route is `provider/model-v2`
- **THEN** `provider/model-v2` receives `advisory_rank=inf` (no prefix/version transfer)

### Requirement: Arena and AA metrics remain separate
Arena scores (0.4–0.98 normalized) and AA coding scores (0–100) SHALL remain separate source components in ranking. They SHALL NOT be mixed arithmetically or compared directly.

#### Scenario: Coding task uses AA coding rating; rankings use Arena
- **WHEN** task category is "coding" and candidate has AA coding rating 85 and Arena ranking score 0.72
- **THEN** rating component uses AA 85; advisory_rank component uses Arena 0.72; no arithmetic combination

#### Scenario: Debugging task uses TEXT leaderboard; ratings use AA
- **WHEN** task category is "debugging" and candidate has AA rating and TEXT leaderboard ranking
- **THEN** rating and advisory_rank remain separate metrics; no cross-metric comparison

### Requirement: Safe fallback preserves baseline order with named omission
Missing, malformed, stale (>12h), 404, timeout, generic-gateway, and unmatched ranking data SHALL produce a named omission in the receipt and preserve identical baseline order as if no rankings were fetched.

#### Scenario: Missing rankings (no OmniRoute gateway)
- **WHEN** gateway is generic OpenAI (not OmniRoute)
- **THEN** omission = "rankings_gateway_unsupported"; baseline order unchanged

#### Scenario: Timeout fetching rankings
- **WHEN** rankings fetch exceeds 5s timeout
- **THEN** omission = "rankings_unavailable"; baseline order unchanged

#### Scenario: 404 from rankings endpoint
- **WHEN** `/api/free-provider-rankings` returns 404
- **THEN** omission = "rankings_unavailable"; baseline order unchanged

#### Scenario: Malformed response
- **WHEN** rankings response missing required fields (rankings[], provider, topModel.modelId, score)
- **THEN** omission = "rankings_malformed"; baseline order unchanged

#### Scenario: Stale rankings (>12h)
- **WHEN** rankings snapshot observed_time > 12h ago (per existing metadata freshness policy)
- **THEN** omission = "rankings_stale"; baseline order unchanged

#### Scenario: No matching routes in admitted pool
- **WHEN** rankings fetched but no admitted route_id matches any ranked route_id
- **THEN** omission = "rankings_no_match"; baseline order unchanged

### Requirement: Receipt exposes ranking transparency fields
Selection receipts SHALL include ranking metadata: category, source, observed_time, reliability, usage, and omission. Unknown scores SHALL be `null` not `0`. No `benchmark_as_of` field SHALL be fabricated.

#### Scenario: Receipt includes ranking fields when used
- **WHEN** rankings successfully fetched and applied
- **THEN** receipt includes: `ranking.category`, `ranking.source="omniroute"`, `ranking.observed_time` (ISO), `ranking.reliability` (dict), `ranking.usage` (dict), `ranking.omission=null`

#### Scenario: Receipt includes omission when fallback
- **WHEN** rankings fetch fails for any reason
- **THEN** receipt includes: `ranking.category` (requested), `ranking.source="omniroute"`, `ranking.observed_time` (fetch time), `ranking.reliability=null`, `ranking.usage=null`, `ranking.omission` (named value)

#### Scenario: Unknown scores are null not zero
- **WHEN** candidate has no AA rating and no rankings match
- **THEN** receipt `rating=null` and `advisory_rank=null` (not 0)

#### Scenario: No benchmark_as_of fabricated
- **WHEN** rankings fetched from API that does not expose benchmark_as_of
- **THEN** receipt does NOT contain `benchmark_as_of` field

## MODIFIED Requirements

### Requirement: Hard admission gates remain authoritative (from existing FR-001, FR-002, FR-006, FR-010)
Authentication, entitlement, capability, quota, capacity, availability, authority, and risk gates SHALL remain authoritative. An excluded top-ranked route SHALL NOT win selection.

#### Scenario: Excluded top-ranked cannot win
- **WHEN** top-ranked route fails any admission gate (quota exhausted, authentication failed, capability missing, capacity filter, cooldown, authority expired)
- **THEN** route is excluded before ranking; next eligible route selected

#### Scenario: Explicit model choice beats advisory ranking
- **WHEN** explicit `--model` provided and eligible
- **THEN** explicit model selected regardless of advisory rank

#### Scenario: Free-only capacity filter beats advisory ranking
- **WHEN** `--capacity free` specified and top-ranked route is subscription
- **THEN** subscription route excluded; best free route selected

## REMOVED Requirements
None.

## RENAMED Requirements
None.
