# Tasks — planners from the frontier band of live ratings

## 1. Band

- [x] 1.1 Add the planner-band function: newest-fetch ranking, suffix and alias dedup, ties included, configuration and validation. Verify: unit tests for each band, stale, older-fetch and unrated routes, suffix variants, ties at a cutoff, and invalid configuration.

## 2. Wire every planner path

- [x] 2.1 Make autodev planner selection use the band: narrow only, strongest non-metered first, equal scores cheaper class first, fallback record, two-family failover (failover may come from the fallback band, recorded), fail closed. Verify: autodev wiring tests, including a one-family frontier band whose failover comes from the fallback band, fewer than two families failing closed, a stronger subscription route beating a weaker free one and an equal-score free route beating subscription.
- [x] 2.2 Make the orchestrate planner failover and the certification preflight use the band instead of `PLANNER_MAX_CAPABILITY_TIER`. Verify: orchestrate planner and rehearsal tests.
- [x] 2.3 Add the doctor `planner_supply` warning and the identify `rating_coverage` and `planner_band_families` fields. Verify: doctor and identify tests.
- [x] 2.4 Docs: the configuration and CLI reference describe the bands and the recorded downgrade; the ADR-043 planner note and the ETHOS "In force" planner line are added. Verify: doc-link check.

## 3. Verification

- [x] 3.1 Run the full suite, ruff, mypy strict, reachability, the lint gates and both OpenSpec validators, and get an independent review. Verify: all commands green.
- [ ] (post-merge) 4.1 On installed main, an autodev run and an orchestrate run each record a frontier-band planner (or a recorded fallback), and doctor shows the `planner_supply` row.

## D39 amendment: single-family planner proceeds

- [x] 5.1 When fewer than two model families are admissible, `select_planner_routes` proceeds with the one family and annotates each result with `single_family_planner=True` and `unavailable_families` (family → reason). Zero families still fails closed. Verify: `test_fewer_than_two_families_proceeds_single_family`, `test_single_family_records_unavailable_families_with_reasons`, `test_two_families_no_single_family_flag`, `test_zero_admissible_routes_still_fails_closed`.
- [x] 5.2 `_live_planner_models` passes `exclusion_reasons` from non-admitted verdicts to `select_planner_routes`; docstring updated. Verify: existing autodev planner wiring tests.
- [x] 5.3 ADR-051 D16 amended in-place; D39 added to `docs/decisions/LOG.md`. Verify: doc-link check.
- [ ] (post-merge) 5.4 On installed main, an autodev run with only one admissible family records `single_family_planner: true` and the unavailable families in the receipt.
