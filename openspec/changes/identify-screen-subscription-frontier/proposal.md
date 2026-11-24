## Why

`verdict identify --screen` could not target subscription frontier routes such
as `cc/claude-opus-5-5` for three reasons:

1. **Provider-name mismatch.** `extra_ratings` used `route_identities[rid][0]`
   (the `owned_by` field from `/v1/models`, e.g. `"anthropic"`) as the
   provider.  The `--provider` filter and `usable_providers` use the connection
   namespace (e.g. `"cc"`), so frontier routes were always dropped by the
   provider filter.
2. **Unknown-tier skip.** The tier filter emitted `skipped_unknown_tier` for
   every route whose `_capability_tier` returned `None`, including frontier
   routes whose model IDs are not yet in the classifier, so a new frontier model
   could never be screened.  Dropping the guard for every no-tier route is too
   broad: the screen spends real quota on subscription routes, and a no-tier
   route with no rating evidence may be a weak model that the ceiling exists to
   exclude.  The filter keeps `skipped_unknown_tier` as the default and lets a
   no-tier route through only when fresh ratings place its model in the ADR-051
   planner band.
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
- **Tier filter** (`identify.py`): a route whose tier is a known value above
  the ceiling is still `skipped_capability_tier`.  A route with no tier keeps
  `skipped_unknown_tier` unless its stripped model key (the route id without a
  trailing `:suffix`) is in the ADR-051 planner band, frontier or fallback
  (`fallback_model_keys` includes the frontier keys), as computed by the
  existing `compute_planner_bands` from fresh `aa_intelligence` ratings (newest
  batch, within the 12-hour window that module uses).  The band is computed
  only when `--max-capability-tier` is set.  With no metadata, or only stale
  ratings, the band is empty and every no-tier route is skipped.  Band
  membership only lets a route reach the probe: it is not proof and does not
  admit the route.

## Capabilities

### Modified Capabilities

- **identify screen (extra_ratings)**: subscription frontier routes (in
  inventory, not in live_candidates, provider has usable connection) are
  included in the unrated-route pool and eligible for screening.
- **identify screen (tier filter)**: `--max-capability-tier N` still skips
  routes whose tier is a known value above N.  A route with no tier is skipped
  (`skipped_unknown_tier`) unless its model is in the planner band on fresh
  ratings, in which case it is probed.
- **identify screen (provider namespace)**: the provider recorded for
  extra-ratings routes uses the connection namespace consistently, so
  `--provider` filters work correctly.

## Impact

- `verdict/identify.py` (extra_ratings loop, tier filter with a read-only
  planner-band lookup, docstring)
- `tests/test_identify_screen.py` (5 tests added; the existing unknown-tier
  tests keep their assertions)
- No change to `verdict/planner_band.py`, admission invariants, ranking,
  review, or dispatch.

## Evidence boundary

KNOWN: `tests/test_identify_screen.py` gains 5 tests: a no-tier route in the
band is screened, a no-tier route with stale ratings is skipped, provider
namespace, subscription frontier route, and the opaque guard.  The two existing
tests that cover `skipped_unknown_tier` (`zz/unknown-tier` and `q/zzqx-blob`)
keep their original assertions.  Run against `origin/main`'s `identify.py`, 4 of
the 5 new tests fail; the opaque-route test passes on both and is a regression
guard.  Run against the earlier version of this change that let every no-tier
route through, the two existing unknown-tier tests and the stale-ratings test
fail.

NOT AVAILABLE: live screen confirming `cc/claude-opus-5-5` is probed and
records a tool-proof entry — tracked as a post-merge task.

## Acceptance criteria

- `verdict identify --screen --max-capability-tier 2` gives a no-tier route
  `skipped_unknown_tier` and never probes it, unless its model is in the
  ADR-051 planner band on fresh ratings, in which case it is probed.  A
  no-tier route with stale or missing ratings is skipped.
- `verdict identify --screen --provider cc` includes routes whose
  `live_candidates.provider == "cc"` even when `route_identities` says
  `"anthropic"`.
- `verdict identify --screen` includes subscription frontier routes
  (`cc/claude-opus-5-5`) when the `cc` provider has a usable connection and
  the route is in the inventory.
- Opaque routes (`auto/*`, `combo/*`) are never screened.
- Each behaviour above has a test; the Evidence boundary says which fail without the fix.
