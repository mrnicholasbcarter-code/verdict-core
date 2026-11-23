## Why

`verdict identify --screen` could not target subscription frontier routes such
as `cc/claude-opus-5-5` for three reasons:

1. **Provider-name mismatch.** `extra_ratings` used `route_identities[rid][0]`
   (the `owned_by` field from `/v1/models`, e.g. `"anthropic"`) as the
   provider.  The `--provider` filter and `usable_providers` use the connection
   namespace (e.g. `"cc"`), so frontier routes were always dropped by the
   provider filter.
2. **Unknown-tier skip.** The tier filter emitted `skipped_unknown_tier` for
   every route whose `_capability_tier` returned `None`, including all frontier
   routes whose model IDs are not yet in the classifier.  Screening is the
   mechanism that proves capability; blocking it at the tier filter defeats the
   purpose.
3. **Missing from the candidate pool.** Subscription frontier routes are not
   returned by OmniRoute's auto-combo endpoint.  They appear in the inventory
   (`/v1/models`) but not in `live_candidates`, so the original `extra_ratings`
   loop skipped them entirely.

## What Changes

- **extra_ratings loop** (`identify.py`):
  - Add `is_opaque(rid) or is_non_chat_route(rid)` guard so opaque routes
    (`auto/*`, `combo/*`) are never added.
  - When `live is not None`: use `live.provider` (connection namespace) instead
    of `route_identities` (inventory namespace).
  - When `live is None` (subscription frontier routes): use
    `rid.split("/", 1)[0]` as provider (route-prefix namespace, same as
    `usable_providers`), include only when that provider is in
    `usable_providers`.
- **Tier filter** (`identify.py`): change
  `(tier is None or tier > max_capability_tier)` to
  `tier is not None and tier > max_capability_tier`.  Routes with an unknown
  tier are not filtered; screening is what produces proof of capability.

## Capabilities

### Modified Capabilities

- **identify screen (extra_ratings)**: subscription frontier routes (in
  inventory, not in live_candidates, provider has usable connection) are
  included in the unrated-route pool and eligible for screening.
- **identify screen (tier filter)**: `--max-capability-tier N` now skips only
  routes whose tier is a known value above N.  Routes with an unknown tier
  pass through and are probed.
- **identify screen (provider namespace)**: the provider recorded for
  extra-ratings routes uses the connection namespace consistently, so
  `--provider` filters work correctly.

## Impact

- `verdict/identify.py` (extra_ratings loop, tier filter, docstring)
- `tests/test_identify_screen.py` (2 existing tests updated, 4 new tests added)
- No change to admission invariants, ranking, review, or dispatch.

## Evidence boundary

KNOWN: 4 new tests (unknown-tier filter, provider namespace, subscription
frontier, opaque guard) were red on the unfixed code and green on the fix;
2 existing tests updated to reflect the new tier-filter semantics.

NOT AVAILABLE: live screen confirming `cc/claude-opus-5-5` is probed and
records a tool-proof entry — tracked as a post-merge task.

## Acceptance criteria

- `verdict identify --screen --max-capability-tier 2` probes routes with
  unknown tier (does not emit `skipped_unknown_tier`).
- `verdict identify --screen --provider cc` includes routes whose
  `live_candidates.provider == "cc"` even when `route_identities` says
  `"anthropic"`.
- `verdict identify --screen` includes subscription frontier routes
  (`cc/claude-opus-5-5`) when the `cc` provider has a usable connection and
  the route is in the inventory.
- Opaque routes (`auto/*`, `combo/*`) are never screened.
- Tests for all four scenarios were red on the unfixed code and green on the fix.
