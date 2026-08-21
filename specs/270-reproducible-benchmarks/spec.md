# Feature Specification: Reproducible direct-vs-Verdict benchmarks

## Issue

GitHub issue #270, V1-007 / BENCH-001 minimal.

## Goal

Provide a checked-in, offline benchmark harness that runs the same task through
a direct model baseline and the Verdict route, and emits a deterministic,
machine-readable report covering cost, latency, completion, regression, and
failover behavior.

## User stories

- As an engineer, I can run one command against checked-in fixtures without
  credentials or network access.
- As a reviewer, I can distinguish direct and Verdict results for the same task
  and inspect their cost/latency/completion deltas.
- As a maintainer, I can detect a regression against a checked-in baseline and
  fail CI explicitly when the regression budget is exceeded.
- As an operator, I can verify that a forced transient failure selects the
  declared fallback without duplicating completed work.

## Functional requirements

1. The runner accepts a fixture path and optional integer seed.
2. Each fixture task is evaluated by both a direct baseline and a Verdict route.
3. The report records task identity, route/model identity, cost, latency,
   completion status, regression status, and failover status.
4. Fixture execution is offline and uses deterministic transports/results; no
   provider, router, credential, Ruflo, or autopilot process is required.
5. The same fixture and seed produce the same semantic report. Wall-clock
   timestamps and measured benchmark samples may vary and must be explicitly
   marked as observational.
6. Regression comparison is against a checked-in baseline and fails closed when
   required baseline fields are absent or malformed.
7. Failover is bounded and records the initial and replacement route; completed
   stages are not repeated.
8. Existing local benchmark commands remain compatible.

## Non-goals

- Live provider quality, network latency, or pricing measurement.
- Claiming model quality from a static fixture completion.
- Replacing Verdict eligibility/routing or adding model selection logic to the
  gate.
- Integrating Ruflo, OmniRoute task routing, autopilot, credentials, or remote
  services.

## Acceptance criteria

- `python benchmarks/run_reproducible.py --fixture ... --seed N` produces JSON
  with direct and Verdict observations for every task.
- The report contains cost, latency, completion, regression, and failover
  sections with explicit status values and provenance.
- Two semantic runs with the same seed and fixture agree; changing the seed
  changes only seeded observation values.
- A deliberately exceeded regression budget is reported as failed and the CLI
  exits non-zero with `--fail-on-regression`.
- Focused tests cover happy path, determinism, malformed baseline, regression
  failure, failover, and offline/no-network behavior.
- Repository tests, lint, format, and strict type checks pass.
