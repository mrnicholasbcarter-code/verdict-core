# Planners come from the frontier band of live ratings

## Why

Verdict's planner floor is a hardcoded name classifier (`verdict/classifier.py`) plus `PLANNER_MAX_CAPABILITY_TIER = 2`, and `verdict autodev` has no planner floor at all. The classifier went stale (it rates `gpt-6.2-sol` and `gpt-6.2-astra` as unknown) and autodev planned a real task with a small model. On 2026-11-23 the operator decided that planning uses frontier models chosen from live ratings, not a list, and agreed every rule in ADR-051 over five rounds of researched options.

## What Changes

- The planner pool for autodev, orchestrate and the certification preflight is the frontier band: the top 20% of distinct rated models by fresh `aa_intelligence` among the rated models in Verdict's route inventory, using only the rating source's newest fetch. A `:suffix` variant (`:free`, `:nitro`) shares its model's slot; models tied with the cutoff score are included.
- If the frontier band has no admissible route, the fallback band (top 30%) is used and the downgrade is recorded. With neither, planning fails closed with `no_frontier_or_fallback_planner`.
- Unrated routes, routes rated only in an older fetch, declared tiers and the id classifier no longer qualify a planner.
- Within a band the strongest admissible non-metered route (FREE, UNKNOWN or SUBSCRIPTION) plans. Equal scores go to the cheaper class first, then the higher `aa_coding`. METERED plans only when no non-metered route in the band is admissible, within the CP1 spend limit; until CP1 lands it stays excluded.
- Autodev keeps its two-family planner list. The primary comes from the best band; the other-family failover may come from the fallback band (recorded). Fewer than two admissible families across both bands fails closed.
- `verdict doctor` warns when no admissible route is in the frontier band. `verdict identify` reports rating coverage and the band's family concentration.
- When this merges, ETHOS "In force" gains: "Planners: strongest admissible model in the frontier band; paid last."

## Capabilities

### Modified Capabilities

- `route-selection`: planner eligibility and order.

## Impact

Affected code: autodev planner selection in `verdict/autodev_run.py`, the planner failover in `verdict/orchestration/run.py`, `verdict/orchestration/contracts.py`, `scripts/run_certification_rehearsals.py`, the doctor planner-supply row, the identify report, operator configuration docs and ETHOS. Executors are unchanged.

## Evidence boundary

- **KNOWN:** on 2026-11-23 the metadata store held 70 `aa_intelligence` scores, all from one fetch (02:00Z, source `openrouter.artificial_analysis`). The store records `fetched_at` but not the AA index version. 42 of 159 available routes (26%) had a fresh score. The best score was 64.6 (Claude Fable 5.2). Tier-0 eligible supply was the Claude subscription only (Codex frontier routes were `quota_low`). Autodev planned with `ddgw/gpt-5.6-luna`.
- **INFERRED:** with defaults and suffix variants counted once, today's frontier band is 9 of 45 rated models (cutoff 54.5): claude-fable-5-2, gpt-6-2-astra, gemini-4-neon, gemini-4-carbon, claude-opus-5-5, gpt-6-2-sol, gpt-6-astra, gpt-6-1-sol and claude-sonnet-5-5. The fallback band (14 models, cutoff 46.5) adds gemini-4-hydrogen, gpt-6-1-terra, gemini-4-argon, claude-fable-5-1 and kimi-k3-1-pro. Ranked across all 65 models the feed rates, the bands would have been 13 and 20 (cutoffs 50.0 and 36.5); the operator chose the inventory population.
- **NOT AVAILABLE:** a measured difference in plan quality between frontier and non-frontier planners on this repo. Verified planner receipts are too few to use; a later ADR covers them.

## Acceptance criteria

- A route outside both bands is never a planner, whatever its name or declared tier.
- An admissible frontier-band route is preferred over any fallback-band route.
- Within a band, a higher-rated non-metered route beats a lower-rated one whatever its cost class. Equal scores go to the cheaper class first, then the higher `aa_coding`. A METERED route plans only when no non-metered route in the band is admissible.
- The ranking covers rated models in the route inventory, not every model the feed rates. Only scores from the source's newest fetch are ranked. Suffix variants share one slot. Ties at a cutoff are included.
- A fallback is recorded in the autodev report and the orchestrate receipt, with the required band, the used band, the reason, the model's rank and the band cutoff.
- With no admissible route in either band, autodev and orchestrate fail closed with `no_frontier_or_fallback_planner`.
- Autodev's planner list spans two families. When only one family is admissible in the frontier band, the other-family failover comes from the fallback band and is recorded; with fewer than two families in both bands, autodev fails closed.
- `verdict doctor` warns when no admissible route is in the frontier band and says whether the fallback band is reachable.
- Ratings never re-admit a route that failed admission and never authorize a send.
