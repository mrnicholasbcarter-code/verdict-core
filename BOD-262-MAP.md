# BOD-262: Subscription Usage & Headroom Semantics

## Overview
Implement subscription usage tracking and headroom semantics for Verdict capacity admission. This maps existing cost_ledger subscription pools to provider headroom evidence, enabling canonical admission decisions without live provider calls.

## Context
- Subscription pools: `subscription_credits` (prepaid), `subscription_opportunity` (window-based)
- Existing headroom system: `check_headroom()` returns None for unknown/unavailable
- Capacity contracts: `CapacitySnapshot`, `ConnectionIdentity`, `PoolStatus`
- Admission flow: Gate → Eligibility → Intelligence → Dispatcher

## Semantics
### 1. Pool Identity Preservation
- Track subscription pool ID per provider/account/workspace
- Preserve reset windows (monthly/quota-period boundaries)
- Never conflate different subscription pools

### 2. Headroom Evidence Integration
- Headroom evidence from `check_headroom()` feeds canonical admission
- `UNKNOWN_HEADROOM` treated as explicit unknown (requires bounded confirmation)
- Stale evidence rejected (TTL enforced)
- Fresh evidence always overrides stale

### 3. Exhaustion Classification
| Pool Status | Admission Action | Evidence Required |
|-------------|------------------|-------------------|
| exhausted | Hard drop | Current snapshot |
| constrained | Allow with warning | Current snapshot + headroom_pct |
| available | Allow | Current snapshot |
| cooldown | Temporary block | Current snapshot + cooldown_until |
| unknown | Bounded confirmation | Fresh evidence within TTL |

### 4. Distinct Reasons
- rate_limit: 429 responses, per-provider limits
- concurrency: simultaneous usage beyond capacity
- overload: provider system overload
- subscription_exhaustion: subscription_credits pool empty
- lockout: account suspended or payment overdue
- payment: subscription renewal failed

## Implementation Notes
- Use existing `cost_ledger` subscription_budgets for tracking
- Extend `capacity_resolve.py` to handle subscription-specific logic
- Add `check_headroom_subscription()` wrapper for admission flow
- Preserve existing `UNKNOWN_HEADROOM` sentinel behavior
- Add mutation-killing tests for each exhaustion scenario

## Test Coverage
- `test_subscription_exhaustion_hard_drop`
- `test_subscription_headroom_unknown_confirmation`
- `test_subscription_stale_evidence_rejection`
- `test_subscription_pool_identity_preservation`
- `test_subscription_multiple_pool_support`

## Related Files
- `verdict/headroom.py` (existing)
- `verdict/capacity_resolve.py` (conflict resolution)
- `verdict/cost_ledger.py` (subscription pools)
- `tests/test_headroom.py` (existing tests)
- New tests in `tests/test_subscription_headroom.py`
