# Tasks

## 1. Implementation

- [x] 1.1 Failing test: under `--max-capability-tier N`, a no-tier route whose model is in the planner band is screened, not given `skipped_unknown_tier`
- [x] 1.2 Fix tier filter: a no-tier route passes only when its stripped model key is in the planner band's fallback set (frontier included) on fresh ratings, otherwise it keeps `skipped_unknown_tier`; a known tier above N is still `skipped_capability_tier`
- [x] 1.3 Failing test: extra-ratings provider uses connection namespace (`live.provider`) for routes in live_candidates
- [x] 1.4 Failing test: subscription frontier route (in inventory, not in live_candidates, provider has usable connection) appears in screen candidates
- [x] 1.5 Fix extra_ratings loop: add opaque/non-chat guard; use `live.provider` for existing path; add else branch using route-prefix provider for subscription routes
- [x] 1.6 Regression test: opaque inventory routes (`auto/*`) are never screened
- [x] 1.7 Existing unknown-tier tests (`zz/unknown-tier`, `q/zzqx-blob`) keep their original assertions: `skipped_unknown_tier`, never probed
- [x] 1.8 Update the `screen_models` docstring: a no-tier route passes only when its model is in the planner band on fresh ratings
- [x] 1.9 Failing test: a no-tier route with stale ratings keeps `skipped_unknown_tier` and is not probed
- [x] 1.10 Failing test (#1316): an inventory route listed in `cooled_models` is not screened and not probed, in and out of the live pool
- [x] 1.11 Failing test (#1316): an inventory route whose row marks it non-chat (type or capability), with a neutral id, is not screened or probed
- [x] 1.12 Fix extra_ratings loop (#1316): skip `cooled_models` routes and apply the row-aware `is_non_chat_route(rid, row)` in addition to the id-only test, so the screened set only narrows; a regression test keeps an id-marked route with an explicit chat row excluded
- [ ] (post-merge) `verdict identify --screen` probes `cc/claude-opus-5-5` and records a tool-proof entry on a live install with a CC subscription connection
