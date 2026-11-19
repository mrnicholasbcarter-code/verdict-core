## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta
- [x] 1.2 `openspec validate chaos-rehearsal-private-proof --strict` passes

## 2. Rehearsal script

- [x] 2.1 Failing tests, with stubbed children and the real `cli._orchestrate` and `build_selector` resolving the paths: chaos reads a cache holding the preflight's passes; selector, permits and refresh resolve one private file; a simulated injected write leaves the shared cache bytes unchanged; the seeded copy equals the source bytes; a missing, invalid or proof-less cache fails closed before chaos
- [x] 2.2 Pin tests: the clean command and environment are unchanged; the evidence directory holds run files only
- [x] 2.3 `_seed_chaos_state` copies the shared cache into `verdict-certification-chaos/`; the chaos command gets `--state-file` and `VERDICT_HEALTH_CACHE`
- [x] 2.4 Docs: `docs/certification/README.md`

## 3. Live

- [ ] (post-merge) 3.1 The certification rehearsal workflow's chaos run on main plans from the seeded proof and reaches its injected faults
