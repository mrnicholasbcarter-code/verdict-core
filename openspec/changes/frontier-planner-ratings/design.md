# Design — planners from the frontier band of live ratings

## Context

See ADR-051. Planner selection exists in two places: autodev's live planner selection and orchestrate's planner failover (which reads `PLANNER_MAX_CAPABILITY_TIER`); the certification rehearsal preflight mints planner proof with the same constant.

## Goals / Non-Goals

Goals: one planner-band function used by every planner path; bands from fresh ratings; one-band fallback recorded; fail closed beyond that.
Non-goals: executor cost order and metered spend limits (CP1); replacing the id classifier for worker sufficiency gates (follow-up); improving rating coverage.

## Decisions

1. Add `planner_band(route, metadata, now)` returning `frontier`, `fallback` or `None`, using `rate_model(..., task_family="specification")` (metric `aa_intelligence`) and only `basis == "score"` (fresh, joined). Rank distinct rated models (aliases of one metadata record count once) across the whole snapshot; the frontier band is the top `frontier_top` fraction, the fallback band the top `fallback_top` fraction (ties at a cutoff are included).
2. Bands read `VERDICT_PLANNER_FRONTIER_TOP` / `VERDICT_PLANNER_FALLBACK_TOP` (defaults 0.20 / 0.30, `0 < frontier <= fallback <= 1`); invalid values fail closed with a named error.
3. Selection narrows the already-admitted planner candidates to the frontier band, else the fallback band; it never adds candidates. Order within a band: capacity (FREE, UNKNOWN, SUBSCRIPTION), then rating desc, then route id.
4. Record `planner_band` on every selection: `{required: "frontier", used, reason, rank, rated_models, cutoff, rating, metric, as_of}`.

## Risks / Trade-offs

- Thin rating coverage narrows planners; the fallback band and the named failure make this visible.
- A stale metadata refresh empties both bands; the doctor metadata row already reports staleness.

## Security / trust implications

Ratings are third-party data used only to narrow; they cannot admit or authorize. Provenance (source, as_of) is recorded with each choice.

## Routing / context / memory implications

Planners move from small free models to frontier subscription models; executors keep cheapest-sufficient routing, so paid quota use grows only by planning calls.

## Migration / rollback

Revert the change to restore the tier floor; no state migration. A zero or inverted band is rejected rather than treated as "no floor".

## ADR impact

New ADR-051, amending the "ratings never change membership" rule for the planner role only.
