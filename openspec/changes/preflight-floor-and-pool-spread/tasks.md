## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec deltas
- [x] 1.2 `openspec validate preflight-floor-and-pool-spread --strict` passes

## 2. Screen

- [x] 2.1 Failing tests: a route below the context floor is never probed and takes no slot; unknown context and unknown tools are skipped; aliased prefixes share one pool count; pools are covered breadth-first; a skipped route takes no pool slot; invalid values are rejected
- [x] 2.2 Golden test: without the new options the screen output matches main
- [x] 2.3 Add `--min-context-tokens`, `--require-tools` and `--max-per-pool` to `identify --screen`

## 3. Rehearsal preflight

- [x] 3.1 Failing tests: the preflight argv carries the three options from the constants; passes in 2 pools fail closed and orchestrate does not run; 3 pools proceed; two passes in one aliased pool count as one; the artifact holds numbers only
- [x] 3.2 `_run_preflight` passes the floor and the pool cap; `preflight_summary` counts pools; fewer than `PREFLIGHT_MIN_PLANNER_POOLS` fails closed
- [x] 3.3 Docs: `docs/CLI_REFERENCE.md`, `docs/certification/README.md`
- [x] 3.4 Local proof, preflight only, on the operator gateway with an empty `VERDICT_HOME`: no slot goes to a route below the floor and every probe is in a different pool

## 4. Live

- [ ] (post-merge) 4.1 The certification rehearsal workflow's preflight on main reports at least 3 planner-eligible backend pools
