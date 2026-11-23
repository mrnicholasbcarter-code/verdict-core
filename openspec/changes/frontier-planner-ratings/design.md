# Design — planners from the frontier band of live ratings

## Context

See ADR-051 (accepted 2026-11-23). Planner selection lives in two places: autodev's live planner selection, and orchestrate's planner failover, which reads `PLANNER_MAX_CAPABILITY_TIER`. The certification rehearsal preflight mints planner proof with the same constant. ADR-043 `strongest_first` currently keys on capacity class first.

## Goals / Non-Goals

Goals:
- One planner-band function used by every planner path.
- Bands built from the newest fetch of fresh ratings.
- Strongest-first selection among non-metered routes.
- A one-band fallback that is recorded, then fail closed.
- A doctor warning on planner supply.

Non-goals:
- Executor cost order and metered spend limits (CP1).
- Verdict's own planner receipts as evidence (a later ADR).
- Replacing the id classifier for worker sufficiency gates (a follow-up).
- Raising rating coverage.

## Decisions

1. Add `planner_band(route, metadata, now)`, which returns `frontier`, `fallback` or `None`. It uses `rate_model(..., task_family="specification")` (metric `aa_intelligence`) and accepts only `basis == "score"` (fresh and exactly joined).
2. Rank the rated models that the route inventory carries (all inventory routes, not only admitted ones), using only the newest `fetched_at` batch of the rating source. A score that exists only in an older batch is unrated. The identify report shows n and both cutoffs so population drift is visible.
3. Count distinct models once. Strip a trailing `:suffix` from the model key, and give aliases of one metadata record a single slot.
4. The frontier band is the top `frontier_top` fraction (ceil) and the fallback band the top `fallback_top` fraction. Every model tied with the cutoff score is included.
5. The bands read `VERDICT_PLANNER_FRONTIER_TOP` and `VERDICT_PLANNER_FALLBACK_TOP` (defaults 0.20 and 0.30, with `0 < frontier <= fallback <= 1`). Invalid values fail closed with a named error.
6. Selection narrows the already-admitted planner candidates to the frontier band, else to the fallback band. It never adds candidates. The order within a band is:
   - non-metered before METERED;
   - then rating, descending;
   - then cost class (FREE or UNKNOWN before SUBSCRIPTION);
   - then `aa_coding`, descending;
   - then route id.

   METERED stays excluded until CP1 supplies a spend limit. This replaces ADR-043's capacity-first key for the planner role only.
7. Record `planner_band` on every selection: `{required: "frontier", used, reason, rank, rated_models, cutoff, rating, metric, fetched_at}`.
8. The doctor row `planner_supply` warns when no admissible route is in the frontier band and names whether the fallback band is reachable. The identify report adds `rating_coverage` and `planner_band_families`.

## Risks / Trade-offs

- Thin rating coverage narrows the planner pool. The fallback band, the doctor warning and the named failure make this visible.
- A new frontier model cannot plan until a fetch rates it. The operator accepted this.
- A stale metadata refresh empties both bands. The doctor metadata row already reports staleness.
- Strongest-first spends subscription quota on planning when a stronger subscription model beats a free one. Equal scores still go to the cheaper class.

## Security / trust implications

Ratings are third-party data and are used only to narrow. They cannot admit or authorize a route. Provenance (source and `fetched_at`) is recorded with each choice.

## Routing / context / memory implications

Planners move from small free models to frontier models, today mostly the Claude subscription. Executors keep cheapest-sufficient routing, so the only new paid-quota use is planning calls.

## Migration / rollback

Revert the change to restore the tier floor. No state migration is needed. A zero or inverted band is rejected rather than treated as "no floor".

## ADR impact

- New ADR-051 (accepted). For the planner role only, it amends ADR-046's rule that ratings never change membership.
- For planners, ADR-043 `strongest_first` changes from capacity-first to non-metered-then-rating.
- ETHOS "In force" gains a planner line at merge.
