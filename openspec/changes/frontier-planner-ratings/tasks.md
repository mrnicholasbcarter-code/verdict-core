# Tasks — planners from the frontier band of live ratings

## 1. Band

- [ ] 1.1 Add the planner-band function: newest-fetch ranking, suffix and alias dedup, ties included, configuration and validation. Verify: unit tests for each band, stale, older-fetch and unrated routes, suffix variants, ties at a cutoff, and invalid configuration.

## 2. Wire every planner path

- [ ] 2.1 Make autodev planner selection use the band: narrow only, strongest non-metered first, equal scores cheaper class first, fallback record, fail closed. Verify: autodev wiring tests, including a stronger subscription route beating a weaker free one and an equal-score free route beating subscription.
- [ ] 2.2 Make the orchestrate planner failover and the certification preflight use the band instead of `PLANNER_MAX_CAPABILITY_TIER`. Verify: orchestrate planner and rehearsal tests.
- [ ] 2.3 Add the doctor `planner_supply` warning and the identify `rating_coverage` and `planner_band_families` fields. Verify: doctor and identify tests.
- [ ] 2.4 Docs: the configuration and CLI reference describe the bands and the recorded downgrade; the ADR-043 planner note and the ETHOS "In force" planner line are added. Verify: doc-link check.

## 3. Verification

- [ ] 3.1 Run the full suite, ruff, mypy strict, reachability, the lint gates and both OpenSpec validators, and get an independent review. Verify: all commands green.
- [ ] (post-merge) 4.1 On installed main, an autodev run and an orchestrate run each record a frontier-band planner (or a recorded fallback), and doctor shows the `planner_supply` row.
