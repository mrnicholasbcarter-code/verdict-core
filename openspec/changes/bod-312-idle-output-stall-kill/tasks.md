# Tasks

## 1. Implementation

- [x] 1.1 Create OpenSpec change (proposal, design, tasks, spec delta) and validate it
- [x] 1.2 Write the failing test `tests/test_orch_idle_stall.py` (silent worker killed within N+5 s, `stalled`, attempt 2 on a different route; env bound never disables the kill)
- [x] 1.3 Stream worker output in `PrimeHeadlessExecutor` and kill the process group on idle output
- [x] 1.4 Classify `stalled` in `FailureIntelligence` and map it in receipt, session ledger and health feedback
- [x] 1.5 Document `VERDICT_WORKER_IDLE_SECONDS` in `docs/CONFIGURATION.md`
- [x] 1.6 Run focused tests, the full suite, ruff, mypy, reachability, OpenSpec checks
- [x] 1.7 Real `verdict orchestrate` run with a silent fake worker; save run dir and receipt as held evidence
- [ ] (post-merge) 1.8 Install from merged main and repeat the real run on the operator's setup
