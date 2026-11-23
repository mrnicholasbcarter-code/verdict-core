# ADR-051: Planners come from the frontier band of live ratings

**Status:** Accepted — every decision agreed with the operator (2026-11-23, five rounds); not yet implemented
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

The operator agreed the decisions marked *agreed* on 2026-11-23, over five
rounds of options. The research behind them is the 2026-11-23 frontier-planner
research note (AA Intelligence Index v5.0.1, live metadata snapshot).
Decisions 3, 4 and 7 come from the earlier draft and restate existing
invariants (narrow-only admission, ratings joined exactly, executors unchanged).

1. **Planning uses frontier models (agreed).** A planner (autodev, orchestrate,
   and the certification preflight that mints planner proof) must come from the
   *frontier band*: the top 20% of distinct rated models by fresh
   `aa_intelligence` (AA Intelligence Index). The ranking covers the rated
   models that Verdict's route inventory carries, not only admitted routes and
   not every model the feed rates (agreed, round 4). On 2026-11-23, with `:free`
   variants counted once, that was 9 of 45 rated models (cutoff 54.5); the
   feed rated 65 distinct models, which would have given 13 (cutoff 50.0). A percentile band was chosen over a fixed score floor
   (goes stale, breaks when AA rescales an index version) and over top-K (ignores
   score gaps).
2. **One-band fallback, then fail closed (agreed).** If no admissible route is in
   the frontier band, use the *fallback band*, the top 30% (14 of 45 models,
   cutoff 46.5, on 2026-11-23), and record the downgrade (required band, used
   band, reason, model rank, cutoff) in the run report and receipt. With nothing
   admissible in either band, fail closed with `no_frontier_or_fallback_planner`
   and say why. There is no third band. A newly released model without a rating
   cannot plan until it is rated.
3. **Ratings, not names.** A route without a fresh, exactly joined rating is in
   neither band. Declared tiers and the id classifier no longer decide planner
   eligibility.
4. **Narrow only.** Ratings remove routes from the planner pool; they never admit a
   route that failed admission (health, tool proof, context, capacity policy) and
   never authorize a send.
5. **Strongest first, cost-capped (agreed).** Inside a band, the highest-rated
   admissible non-metered route plans; cost class does not reorder non-metered
   routes. A METERED route plans only when no non-metered route in the band is
   admissible, and only within the CP1 (#1244) spend limit; until CP1 lands,
   METERED planners stay excluded. This amends ADR-043 `strongest_first` for the
   planner role: capacity class stops being the primary key among non-metered
   routes.
6. **No family-diversity guard; track concentration (agreed).** The band is pure
   rating. Doctor and identify report the planner band's family concentration
   (on 2026-11-23: three families, Anthropic, OpenAI and Google, with the
   admissible supply mostly the Claude subscription) as a supply metric.
7. **Executors are unchanged by this ADR.** They keep the cheapest-sufficient order;
   the operator's 2026-11-23 rule that executors may use paid routes as a last
   resort is implemented by CP1.

8. **One slot per model (agreed, round 2).** A suffix after `:` (`:free`,
   `:nitro`, `:turbo`) names a tier or speed of the same model and does not get
   its own ranking slot; the variants share the model's slot and score.
9. **Ties are included (agreed, round 2).** Every model whose score equals the
   cutoff score is in the band; the band grows only by the size of the tie.
10. **UNKNOWN counts as non-metered (agreed, round 2).** For decision 5,
    UNKNOWN-capacity routes compete on rating with FREE and SUBSCRIPTION routes,
    as ADR-049 `assume_free` provides.
11. **ETHOS gains a planner line (agreed, round 2).** When the implementation
    merges (not before, because ETHOS states only what runs), ETHOS "In force"
    keeps "Cheapest qualified tier first, then strongest" and adds "Planners:
    strongest admissible model in the frontier band; paid last."

12. **Newest fetch only (agreed, round 3).** The ranking uses only scores from
    the rating source's newest fetch. A model missing from that fetch is
    unrated until a later fetch includes it. One fetch is one published
    leaderboard, so scores from two AA index versions are never ranked
    together. The metadata store records `fetched_at`, not the index version;
    on 2026-11-23 all 70 `aa_intelligence` scores came from one fetch.
13. **Ratings only; receipt evidence is a later ADR (agreed, round 3).**
    Verdict's own verified planner receipts do not change the band yet. When
    enough exist (the research suggested 50 or more per model), a separate
    Proposed ADR will research and agree how they count.
14. **Equal scores: cheaper class first (agreed, round 3).** When two admissible
    planners in a band have the same `aa_intelligence`, FREE and UNKNOWN come
    before SUBSCRIPTION, then the higher `aa_coding` wins.
15. **Doctor warns on planner supply (agreed, round 3).** `verdict doctor` warns
    when no admissible route is in the frontier band, and says whether only the
    fallback band is reachable or neither is. `verdict identify` reports
    rating coverage (26% of available routes had a fresh score on 2026-11-23)
    and the band's family concentration. There is no coverage % gate.
16. **Two-family failover keeps working (agreed, round 5).** Where a planner
    path keeps a failover list (autodev today: at least two model families, so
    one provider's outage cannot end planning, BOD-393), the primary planner is
    the strongest admissible route in the best band available. The
    other-family failover comes from the frontier band when one is admissible,
    otherwise from the fallback band, and the receipt records the band of each
    planner. With fewer than two families admissible across both bands,
    planning fails closed and the reason names how many families were found.

## Consequences

- Planner quality tracks the live leaderboard without code edits when new models ship.
- The band ranks against the rated models in the route inventory, so adding many
  rated small models (for example through community discovery) raises n and can
  move the cutoffs toward mid-tier models. `verdict identify` reports n and both
  cutoffs, so the drift shows; changing the population needs a new decision.
- Thin rating coverage narrows the planner pool; unrated models cannot plan.
  Rating coverage becomes a supply metric worth improving.
- Today the frontier band's admissible supply is the Claude subscription (Codex
  frontier routes were `quota_low`), so the fallback band will be used when Claude
  is unavailable.
- The bands are operator configuration (`VERDICT_PLANNER_FRONTIER_TOP`, default
  0.20; `VERDICT_PLANNER_FALLBACK_TOP`, default 0.30), validated to
  `0 < frontier <= fallback <= 1`. Aliases of one model count once in the ranking.
