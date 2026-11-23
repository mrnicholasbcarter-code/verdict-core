# Tasks — planners from the frontier band of live ratings

## 1. Band

- [ ] 1.1 Add the planner-band function, configuration and validation. Verify: unit tests for bands, stale and unrated routes, invalid configuration.

## 2. Wire every planner path

- [ ] 2.1 Autodev planner selection uses the band (narrow only, order within band, fallback record, fail closed). Verify: autodev wiring tests.
- [ ] 2.2 Orchestrate planner failover and the certification preflight use the band instead of `PLANNER_MAX_CAPABILITY_TIER`. Verify: orchestrate planner and rehearsal tests.
- [ ] 2.3 Docs: configuration and CLI reference describe the bands and the recorded downgrade. Verify: doc-link check.

## 3. Verification

- [ ] 3.1 Full suite, ruff, mypy strict, reachability, lint gates, both OpenSpec validators; independent review. Verify: commands green.
- [ ] (post-merge) 4.1 On installed main, an autodev run and an orchestrate run each record a frontier-band planner (or a recorded fallback).
