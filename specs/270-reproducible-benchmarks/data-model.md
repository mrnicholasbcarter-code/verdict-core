# Data model: issue #270

## Fixture

```text
BenchmarkFixture
  schema_version: string
  seed: integer
  tasks: list[BenchmarkTask]
  baseline: BaselineMetrics
  regression_budget: RegressionBudget
  failover: FailoverFixture
```

```text
BenchmarkTask
  id: non-empty string
  prompt: non-empty string
  criticality: string
  direct: RouteObservationFixture
  verdict: RouteObservationFixture
```

```text
RouteObservationFixture
  model: non-empty string
  provider: non-empty string
  cost_usd: non-negative number
  latency_ms: positive number
  completion: one of success|failure
```

## Report

```text
BenchmarkReport
  schema_version: string
  mode: local-reproducible
  seed: integer
  fixture_digest_sha256: digest
  tasks: list[TaskBenchmark]
  aggregate: AggregateMetrics
  regression: RegressionResult
  failover: FailoverResult
  provenance: Provenance
  metrics: existing local microbenchmark metrics
```

`TaskBenchmark` contains direct and Verdict observations plus deltas. Aggregate
contains total cost, median latency, completion counts, and task count.
`RegressionResult` contains baseline digest, observed metric, budget, and an
explicit `passed` boolean. `FailoverResult` contains initial model,
replacement model, trigger, completed stages, and duplicate-stage count.

## Invariants

- All monetary and latency values are finite and non-negative; latency is
  positive.
- Task IDs are unique and report order follows fixture order.
- Baseline task IDs must exactly match fixture task IDs.
- A failed regression cannot be represented as passed.
- Failover passes only when the replacement is different, the trigger is
  transient, and duplicate completed stages equal zero.
- Raw credentials, prompts, completions, and provider network payloads are not
  written to the report.
