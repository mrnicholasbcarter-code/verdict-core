## Why

Verdict routes prompts to the cheapest model proven capable of a task (BOD-342
D15).  To know which free and subscription routes are actually capable, we need
live probe evidence: HTTP liveness plus a tool-call check at agent context size.
Today no Verdict-native prober exists; operators must run a separate freescan
scan and manually cross-reference results.  This change adds the prober and a
12-hour proof registry inside the main repo, enabling `verdict discovery refresh`
to gate route selection on observed capability.

## What Changes

A new `verdict/discovery/` package ports freescan's standard-library prober into
Verdict.  It adds:

- `verdict/discovery/model.py` — shared `Route`, `ProbeResult`, `Score` types and
  status constants (`UP`, `NO_KEY`, `AUTH_ERROR`, `RATE_LIMITED`, `DOWN`,
  `TIMEOUT`, `GARBAGE`, `ERROR`).
- `verdict/discovery/probe.py` — parallel liveness prober with injectable
  transport for offline tests.
- `verdict/discovery/toolprobe.py` — tool-call correctness check at agent context
  size (~18 k tokens of padding).
- `verdict/discovery/scores.py` — coding-quality scores from OpenRouter and
  free-coding-models benchmarks (cached, offline-safe).
- `verdict/discovery/registry.py` — 12-hour proof registry at
  `$VERDICT_HOME/discovery/registry.json`, written atomically; per-pool circuit
  breaker on 429.
- `verdict/discovery/refresh.py` — CLI action: load probeable routes, run
  refresh, render plain or JSON output.
- `verdict/discovery/omniroute_source.py` — OmniRoute catalog adapter for
  refresh.

A new `verdict discovery refresh [--json] [--dry-run]` subcommand is wired
through `verdict/commands/parsers_discovery.py` and
`verdict/commands/dispatch.py`.

## Capabilities

### New Capabilities

- `discovery-prober`: Parallel liveness and tool-call prober for free and
  subscription routes with injectable transport for offline tests.
- `discovery-registry`: 12-hour proof registry at
  `$VERDICT_HOME/discovery/registry.json`, written atomically, with per-pool
  circuit breaker.

### Modified Capabilities

- `discovery-funnel` (read-only extension): `verdict discovery refresh` adds
  the `refresh` subcommand alongside the existing `funnel` subcommand.  No
  existing funnel behaviour is changed.

## Impact

New package `verdict/discovery/` (8 files).  Touched:
`verdict/commands/parsers_discovery.py` (new `refresh` subparser),
`verdict/commands/dispatch.py` (new dispatch branch).  No existing behaviour
changes.  Probe ONLY routes classified FREE or SUBSCRIPTION by a fact; never
probe unknown or priced routes.  All tests are offline (no live provider calls).
