## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta
- [x] 1.2 `openspec validate chaos-supply-preflight --strict` passes

## 2. Rehearsal script

- [x] 2.1 Failing tests: the preflight argv carries `--max-routes 13`; 4 proven pools (the cert-fix8 shape) fail closed before any orchestrate run with "5 are required"; the chaos cache and ledger equal the shared files as they stood right after the preflight, and a clean-run write to either does not reach chaos; a missing ledger seeds an empty one; a cache without proof stops before the clean run
- [x] 2.2 `PREFLIGHT_MAX_ROUTES = 13`, `PREFLIGHT_MIN_PLANNER_POOLS = 5`; `_seed_chaos_state` runs before the clean run and copies the ledger; the chaos command gets `VERDICT_SESSION_EVIDENCE`
- [x] 2.3 Docs: `docs/certification/README.md`

## 3. Live

- [ ] (post-merge) 3.1 The certification rehearsal workflow on main completes both rehearsals
