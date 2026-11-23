# ADR-051: Planners come from the frontier band of live ratings

**Status:** Accepted (operator, 2026-11-23)
**Date:** 2026-11-23
**Deciders:** Operator
**Amends:** the recorded rule that model ratings are advisory tie-breaks that "cannot change membership" — for the planner role only. See [ADR-046](ADR-046-arena-ratings-advisory-source.md).
**Relates to:** [ADR-043](ADR-043-role-dependent-selection-order.md) (role-dependent order),
[ADR-049](ADR-049-cost-policy-and-unknown-handling.md) (cost classes and order),
[ADR-012](ADR-012-consented-budgeted-probes.md) (bounded probes)

## Context

`verdict orchestrate` admits a planner whose capability tier is 2 or better
(`PLANNER_MAX_CAPABILITY_TIER = 2`); `verdict autodev` has no planner floor and on
2026-11-23 planned a coding task with a small free model. Capability tier comes from
a declared inventory value or from `verdict/classifier.py`, a regex list of model
names. That list goes stale: on 2026-11-23 it rated `gpt-6.2-sol` and
`gpt-6.2-astra` as unknown, although both are current frontier models.

Verdict already holds fresh, source-attributed ratings (Artificial Analysis via the
metadata store, 12-hour freshness) for the frontier models. On 2026-11-23 the best
`aa_intelligence` score was 64.6 (Claude Fable 5.2), with GPT-6.2 Astra 62.1,
Gemini 4 Neon 61.4, Claude Opus 5.5 59.1 and GPT-6.2 Sol 57.3. 88 of 642 routes
were rated.

## Decision

1. **Planning uses frontier models.** A planner (autodev, orchestrate, and the
   certification preflight that mints planner proof) must come from the
   *frontier band*: routes whose fresh planner rating is at least
   `frontier_band x best`, where `best` is the highest fresh rating for the planner
   metric across the whole metadata snapshot, not only across admitted routes.
   Default `frontier_band = 0.90`.
2. **One-band fallback.** If no admissible route is in the frontier band, use the
   *fallback band* (`fallback_band x best`, default `0.80`), and record the
   downgrade (required band, used band, reason, best score, threshold) in the run
   report and receipt. With nothing in either band, fail closed with a named reason.
3. **Ratings, not names.** A route without a fresh, exactly joined rating is in
   neither band. Declared tiers and the id classifier no longer decide planner
   eligibility.
4. **Narrow only.** Ratings remove routes from the planner pool; they never admit a
   route that failed admission (health, tool proof, context, capacity policy) and
   never authorize a send.
5. **Cost order within a band.** Inside a band: FREE, then UNKNOWN, then
   SUBSCRIPTION (ADR-049 order), then rating. METERED planners stay excluded until
   the cost policy (CP1, #1244) supplies a spend limit; then they rank last within a
   band.
6. **Executors are unchanged by this ADR.** They keep the cheapest-sufficient order;
   the operator's 2026-11-23 rule that executors may use paid routes as a last
   resort is implemented by CP1.

## Consequences

- Planner quality tracks the live leaderboard without code edits when new models ship.
- Thin rating coverage narrows the planner pool; unrated models cannot plan.
  Rating coverage becomes a supply metric worth improving.
- Today the frontier band's admissible supply is the Claude subscription (Codex
  frontier routes were `quota_low`), so the fallback band will be used when Claude
  is unavailable.
- The bands are operator configuration (`VERDICT_PLANNER_FRONTIER_BAND`,
  `VERDICT_PLANNER_FALLBACK_BAND`), validated to `0 < fallback <= frontier <= 1`.
