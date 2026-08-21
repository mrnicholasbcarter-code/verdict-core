# Implementation plan: issue #270

## Goal

Add a minimal offline direct-vs-Verdict benchmark contract to the existing
reproducible harness, with explicit regression and failover evidence.

## Ordered tasks

1. Add a checked-in benchmark fixture containing two representative tasks,
   baseline aggregate values, regression budget, and forced failover scenario.
   Acceptance: fixture loads and validates through the public runner.
2. Add typed internal validation/normalization helpers and report builders in
   `verdict/benchmarking.py`. Acceptance: malformed fixture/baseline fails with
   a useful `ValueError`; report contains all required sections.
3. Reuse `ComparisonHarness` for direct/Verdict route identity and static
   estimates, deriving seeded deterministic observations. Acceptance: same
   task is represented on both paths and no network is touched.
4. Reuse `run_forced_failover_proof`/`replay_proof` for failover evidence.
   Acceptance: report records fallback and zero duplicate completed stages.
5. Extend `benchmarks/run_reproducible.py` with `--seed` and
   `--fail-on-regression`, retaining existing flags. Acceptance: exit status
   reflects regression result.
6. Add focused tests in `tests/test_benchmarking.py` (or a dedicated test
   module) for schema, determinism, regression, failover, and offline behavior.
7. Update benchmark documentation and run all repository quality gates.

## Files

Modify: `verdict/benchmarking.py`, `benchmarks/run_reproducible.py`, benchmark
docs, and focused tests.

Add: `benchmarks/fixtures/direct_vs_verdict.json` and the Spec Kit artifacts.

## Risks

- Existing report consumers may depend on the current top-level shape: retain
  all existing keys and add fields compatibly.
- Timing noise must not affect semantic regression checks: seed all synthetic
  observations and keep microbench threshold checks separate.
- The branch has unrelated-but-owned seed changes: do not rewrite them.
