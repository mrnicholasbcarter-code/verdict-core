# Tasks

## 1. Implementation

- [x] 1.1 Failing test: `--max-capability-tier N` does not emit `skipped_unknown_tier` for routes with unknown tier
- [x] 1.2 Fix tier filter: `tier is not None and tier > max_capability_tier`
- [x] 1.3 Failing test: extra-ratings provider uses connection namespace (`live.provider`) for routes in live_candidates
- [x] 1.4 Failing test: subscription frontier route (in inventory, not in live_candidates, provider has usable connection) appears in screen candidates
- [x] 1.5 Fix extra_ratings loop: add opaque/non-chat guard; use `live.provider` for existing path; add else branch using route-prefix provider for subscription routes
- [x] 1.6 Regression test: opaque inventory routes (`auto/*`) are never screened
- [x] 1.7 Update existing tests to reflect new tier-filter semantics (2 tests)
- [x] 1.8 Update `screen_models` docstring to remove "unknown or above it"
- [ ] (post-merge) `verdict identify --screen` probes `cc/claude-opus-5-5` and records a tool-proof entry on a live install with a CC subscription connection
