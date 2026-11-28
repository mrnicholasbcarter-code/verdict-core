# Design: ranking admitted routes by verified success

## Context

Admission narrows the set of routes; the ladder then sorts what is left by `_rank_key`. The ledger of verified outcomes exists and the receipt already shows a pooled score, but the sort ignores it. The operator's rule of 2026-11-20 is that a model is judged by how often it succeeds at real tasks, with no second scoring formula, and that cheaper routes that succeed rise.

## Goals / Non-Goals

Goals:

- Within a capacity class, a route that has succeeded rises and a route that has failed falls, for the kind of node being selected.
- No route is admitted, made healthy or moved across a capacity class because of a score.
- The receipt shows what was used and what it was based on.

Non-Goals (left for later slices):

- Risk in the key. The addendum on #1216 asks for node kind plus risk; the ledger has no risk field. Slice 1 keys on kind alone. Adding risk means a ledger field and a third key level.
- Retiring `outcome-records.jsonl` (L1c), exploration of under-sampled routes (#1218), escalation by measured score (#1217).
- Planner and reviewer selections, and `selected_because` in `explain.py` naming the success rank.

## Decisions

1. **Reuse `score_task_history`.** It takes observations (route, family, quality credit, time, id) and returns a smoothed, decayed score with `observed_n`. A ledger row converts to one observation: credit 1.0 for a pass and 0.0 for a fail, the row's time, an id built from route, time and outcome, and the family set to the node kind (or `*` for the route-alone key). No fallback to the plan's `0.5 + min(1, n/N) * (rate - 0.5)` was needed. Differences to know: the scorer's window is 30 days, so the 14-day setting is applied by selecting rows before the call (and may not exceed 30); its half-life weighting means newer rows count more; a false claim is one failure here, where the gate's summary counts it twice (the gate still vetoes a recent false claim, so no second penalty is needed).
2. **Key `route+kind`, then `route`, then neutral.** `kind` is `WorkNode.kind` as recorded in `node_kind`. If the route has at least `min_sample` (default 3, the scorer's demotion floor) verified results for the kind, the score is for that kind. Otherwise, with at least `min_sample` results for every kind, the score is for the route alone. Otherwise the route is neutral, 0.5, with no key. A route is one record across aliases of the same model in one backend pool, the same pooling `summarize_pool` uses, so the receipt's `session_passes` and the new `n` describe the same route.
3. **Position.** Second key in `_rank_key`, in both orderings: capacity class, success rank, then slack (or the rating keys for strongest-first). `_probe_order` takes five key elements instead of four so its order is unchanged apart from the new key.
4. **Resolution.** The sort sees the score in tenths (`-round(score * 10)`). A raw float would let one day of decay between two routes with the same record outrank slack, the cheapest-sufficient rule. The receipt carries the exact score.
5. **Scope: node selections only.** `TaskRequirements.node_kind` is set only by `for_node`. Without a kind (planner, reviewer, benches, the CLI report) every route gets the same neutral value, so the ETHOS planner rule and existing orderings are untouched. The success object then reads `source: no_node_kind` with a null score.
6. **Only verified real work.** Rows must be `verified_by_controller` (the ledger window already requires it) and `kind == "real_task"`; the importer's `session_canary_v1` and `free_real_task` rows do not count.
7. **One extra ledger read per selection.** The success rank reads the ledger once; the existing pooled summary the gate and the receipt's `session_*` fields use still reads per route and is not changed. The ladder holds the grouped rows on the selecting thread, keyed by the time and window they were read for, and drops them at the start of each selection. An unreadable ledger sets the existing per-selection error once; every route is then neutral with `source: unreadable`.
8. **Settings.** `success_window_days` (14, 1 to 30) and `success_min_sample` (3, at least 1) are constructor arguments of the ladder with module constants as defaults, validated at construction. Every candidate's `success` records both. No environment variable is added.
9. **Receipt fields.** `rank_components.success = {score, n, key, node_kind, source, window_days, min_sample}`. `score`/`n` are null when nothing could be read, with the reason in `source`; a neutral route has score 0.5, its n, a null key and `below_min_sample`.

## Risks / Trade-offs

- With one ledger of about 50 rows over 15 routes, most routes have fewer than 3 results per kind, so the route-alone key does most of the work at first, and a failure in one kind lowers a route in another until that kind has its own sample.
- The success rank sits ahead of slack, so a well-performing wider-margin route can outrank the least capable sufficient route in its class. That is the intent of "cheaper routes that succeed rise", but it is a change in who wins ties inside a class.
- Equal tenths fall through to the old order, so a route and a cold route with a score near 0.5 behave as before.
- FREE and UNKNOWN routes with a pooled score under 0.5 are still refused by the gate; the rank adds ordering only for those that pass it, and for SUBSCRIPTION and METERED routes, which the gate does not read.

## Security / trust implications

The score reads a local file the controller writes. It can only reorder routes already admitted; it cannot admit, select a route outside the set, or raise a capacity class. A tampered ledger can therefore change an order within a class but not what may run.

## Routing / context / memory implications

Routing: ranking within a class only. Memory and context packs are untouched; the ledger is the existing store, and no new store is added.

## Migration / rollback

No stored state or schema change. New receipts carry the extra object; readers that ignore unknown keys are unaffected. Reverting restores the old order.

## ADR impact

None.
