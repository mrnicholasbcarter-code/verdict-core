# Tasks

- [x] Port the session sampler and hysteresis into `verdict/host_pressure.py` with tests on fake /proc trees.
- [x] Treat a missing `pressure` in `evaluate_admission` as full pressure; pass the measured level from the supervisor.
- [x] Limit `orchestrate` worker slots by the level, with `HOST_PRESSURE` controller events; never cancel running attempts.
- [x] Refuse a new `improve` cycle at RED or without evidence.
- [x] Scale the prover's agentic concurrency by the level.
- [x] Add `verdict host pressure [--json]` and the doctor section.
- [x] Write ADR-039 and the CLI reference.
- [x] Live acceptance on the operator host from the branch (evidence.md).
- [ ] Live acceptance from merged main (DoD 1), then archive the change.
