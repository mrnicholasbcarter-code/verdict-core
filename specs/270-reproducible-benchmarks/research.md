# Research: issue #270

## Existing capability

- `verdict/comparison.py` already resolves a direct primary model and a Verdict
  route for the same task, with static cost/latency/quality estimates.
- `verdict/benchmarking.py` already provides checked-in local fixtures,
  warmups, nanosecond samples, p95 thresholds, metadata, and a CLI entry point.
- `verdict/failover_replay_proof.py` and `verdict/failover_engine.py` already
  prove bounded transient failover and non-duplication of completed stages.
- `verdict/models.py` and `verdict/gate.py` provide the stable route contracts.

## Decision

Extend the existing benchmark module with a fixture-driven comparison layer and
reuse the existing failover proof contract. Do not introduce a second router or
live transport. Keep the current local microbenchmarks as a separate section so
their latency numbers are not confused with task outcome observations.

## Constraints and risks

- `time.perf_counter_ns` is intentionally observational; semantic comparison
  fields must not depend on it.
- Static fixture completion is a harness assertion, not model-quality evidence.
- Baseline schema errors must fail closed rather than silently producing a
  misleading regression result.
- The existing worktree contains branch-owned benchmark seed changes; preserve
  them and stage only files for #270.

## Verification research

The repository quality contract is `pytest`, `ruff check`, `ruff format --check`,
and `mypy --strict verdict`. Existing benchmark documentation requires fixture
digest, commit, environment, and command provenance.
