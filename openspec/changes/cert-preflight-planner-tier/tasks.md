## 1. Implementation

- [x] 1.1 Failing tests: a tier-3 or unknown-tier route is never probed under `--max-capability-tier`; skipped routes take no cap slot; screens without the flag are unchanged
- [x] 1.2 Add `--max-capability-tier` to `identify --screen`, reusing `eligibility._capability_tier`, and share the planner floor constants with `plan_with_failover`
- [x] 1.3 Failing tests: preflight argv has `--max-capability-tier 2`; passes the planner rejects fail closed and orchestrate does not run; each reason enum; no URL or key reaches the artifact
- [x] 1.4 Preflight counts only planner-eligible passes; classify `run_finished` reasons; write preflight counts to `certification-failure.json`
- [x] 1.5 Docs: `docs/CLI_REFERENCE.md`, `docs/certification/README.md`
- [x] 1.6 Local proof on the operator gateway with an empty `VERDICT_HOME`: preflight finds planner-eligible passes and planning admits a route
- [ ] (post-merge) 1.7 Dispatch `certify-rehearsal.yml` on merged main and verify the clean rehearsal gets past planning
