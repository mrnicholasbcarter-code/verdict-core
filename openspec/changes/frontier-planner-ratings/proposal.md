# Planners come from the frontier band of live ratings

## Why

Verdict's planner floor is a hardcoded name classifier (`verdict/classifier.py`) plus `PLANNER_MAX_CAPABILITY_TIER = 2`, and `verdict autodev` has no planner floor at all. The classifier went stale (it rates `gpt-6.2-sol` and `gpt-6.2-astra` as unknown) and autodev planned a real task with a small model. The operator decided on 2026-11-23 that planning uses frontier models, chosen from live ratings, not a list (ADR-051).

## What Changes

- The planner pool for autodev, orchestrate and the certification preflight is the frontier band: the top 20% of distinct rated models by fresh planner rating across the metadata snapshot.
- If the frontier band has no admissible route, the fallback band (top 30%) is used and the downgrade is recorded; with neither, planning fails closed with a named reason.
- Unrated or stale-rated routes, declared tiers and the id classifier no longer qualify a planner. Within a band: FREE, UNKNOWN, SUBSCRIPTION, then rating; METERED stays excluded until CP1.

## Capabilities

### Modified Capabilities

- `route-selection`: planner eligibility and order.

## Impact

`verdict/autodev_run.py` planner selection, `verdict/orchestration/run.py` / planner failover, `verdict/orchestration/contracts.py`, `scripts/run_certification_rehearsals.py`, operator configuration docs. Executors are unchanged.

## Evidence boundary

- **KNOWN:** on 2026-11-23, 88 of 642 routes had fresh `aa_intelligence` ratings; best 64.6 (Claude Fable 5.2); tier-0 eligible supply was the Claude subscription only (Codex frontier `quota_low`); autodev planned with `ddgw/gpt-5.6-luna`.
- **INFERRED:** with defaults, today's frontier band is 10 of 47 rated models (Fable 5.2, GPT-6.2 Astra, Gemini 4 Neon/Carbon/Hydrogen, Opus 5.5, GPT-6.2 Sol, GPT-6 Astra, GPT-6.1 Sol, Sonnet 5.5) and the fallback band adds 5 more.
- **NOT AVAILABLE:** a measured plan-quality difference between frontier and non-frontier planners on this repo.

## Acceptance criteria

- A route outside both bands is never a planner, whatever its name or declared tier.
- An admissible frontier-band route is preferred over any fallback-band route; within a band the cost order holds.
- A fallback is recorded in the autodev report and the orchestrate receipt with required band, used band, reason, the model's rank and the band cutoff.
- With no admissible route in either band, autodev and orchestrate fail closed with `no_frontier_or_fallback_planner`.
- Ratings never re-admit a route that failed admission and never authorize a send.
