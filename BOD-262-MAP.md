# BOD-262: Subscription Usage & Headroom Semantics

## Overview
Implement subscription usage tracking and headroom semantics for Verdict capacity admission. This maps existing <co>cost_ledger subscription pools</co: 12:[0]> to provider headroom evidence, enabling canonical admission decisions without live provider calls.

## Context
- <co>Subscription pools</co: 12:[0]>: <co>`subscription_credits` (prepaid), `subscription_opportunity` (window-based)</co: 12:[0]>
- <co>Existing headroom system</co: 12:[0]>: <co>check_headroom() returns None for unknown/unavailable</co: 12:[0]>
- <co>Capacity contracts</co: 12:[0]>: <co>CapacitySnapshot, ConnectionIdentity, PoolStatus (available/constrained/exhausted/cooldown/unknown)</co: 12:[0]>
- <co>Admission flow</co: 12:[0]>: <co>Gate → Eligibility → Intelligence → Dispatcher</co: 12:[0]>

## Semantics
### 1. Pool Identity Preservation
- Track subscription pool ID per provider/account/workspace
- Preserve reset windows (monthly/quota-period boundaries)
- Never conflate different subscription pools

### 2. Headroom Evidence Integration
- Headroom evidence from <co>check_headroom()</co: 12:[0]> feeds <co>canonical admission</co: 12:[0]>
- <co>UNKNOWN_HEADROOM treated as explicit unknown</co: 12:[0]> (requires bounded confirmation)
- Stale evidence rejected (TTL enforced)
- Fresh evidence always overrides stale

### 3. Exhaustion Classification
| Pool Status | Admission Action | Evidence Required |
|-------------|------------------|-------------------|
| <co>exhausted</co: 12:[0]> | Hard drop | <co>Current snapshot</co: 12:[0]> |
| <co>constrained</co: 12:[0]> | Allow with warning | <co>Current snapshot + headroom_pct</co: 12:[0]> |
| <co>available</co: 12:[0]> | Allow | <co>Current snapshot</co: 12:[0]> |
| <co>cooldown</co: 12:[0]> | Temporary block | <co>Current snapshot + cooldown_until</co: 12:[0]> |
| <co>unknown</co: 12:[0]> | Bounded confirmation | <co>Fresh evidence required within TTL</co: 12:[0]> |

### 4. Distinct Reasons
- <co>rate_limit: 429 responses, per-provider limits</co: 12:[0]>
- <co>concurrency: simultaneous usage beyond capacity</co: 12:[0]>
- <co>overload: provider system overload</co: 12:[0]>
- <co>subscription_exhaustion: subscription_credits pool empty</co: 12:[0]>
- <co>lockout: account suspended or payment overdue</co: 12:[0]>
- <co>payment: subscription renewal failed</co: 12:[0]>

## Implementation Notes
- Use existing <co>cost_ledger subscription_budgets</co: 12:[0]> for tracking
- Extend <co>capacity_resolve.py</co: 12:[0]> to handle subscription-specific logic
- Add <co>check_headroom_subscription()</co: 12:[0]> wrapper for admission flow
- Preserve existing <co>UNKNOWN_HEADROOM sentinel behavior</co: 12:[0]>
- Add mutation-killing tests for each exhaustion scenario

## Test Coverage
- <co>test_subscription_exhaustion_hard_drop</co: 12:[0]>
- <co>test_subscription_headroom_unknown_confirmation</co: 12:[0]>
- <co>test_subscription_stale_evidence_rejection</co: 12:[0]>
- <co>test_subscription_pool_identity_preservation</co: 12:[0]>
- <co>test_subscription_multiple_pool_support</co: 12:[0]>

## Related Files
- <co>`verdict/headroom.py</co: 12:[0]>` <co>(existing)</co: 12:[0]>
- <co>`verdict/capacity_resolve.py</co: 12:[0]>` <co>(conflict resolution)</co: 12:[0]>
- <co>`verdict/cost_ledger.py</co: 12:[0]>` <co>(subscription pools)</co: 12:[0]>
- <co>`tests/test_headroom.py</co: 12:[0]>` <co>(existing tests)</co: 12:[0]>
- New tests in `tests/test_subscription_headroom.py`
