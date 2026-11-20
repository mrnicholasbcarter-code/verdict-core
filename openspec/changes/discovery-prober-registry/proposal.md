# Discovery prober and registry

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
- `verdict/discovery/probe.py` — per-pool serial liveness prober with injectable
  transport for offline tests.
- `verdict/discovery/toolprobe.py` — tool-call correctness check at agent context
  size (~18 k tokens of padding).
- `verdict/discovery/scores.py` — coding-quality scores from OpenRouter and
  free-coding-models benchmarks (cached, offline-safe).
- `verdict/discovery/registry.py` — 12-hour proof registry at
  `$VERDICT_HOME/discovery/registry.json`, written atomically; per-pool circuit
  breaker on 429; `probe_skip_reason` field distinguishes not-probed routes from
  probed-but-down routes.
- `verdict/discovery/refresh.py` — CLI action: load OmniRoute routes via S2's
  `fetch_omniroute_evidence` + `rows_from_evidence` adapter, run refresh, render
  plain or JSON output.  Non-OmniRoute catalogs (openrouter, kilo, models.dev,
  community) are not probeable through the gateway until a later story.
  `--dry-run` returns `dry_run_requires_catalog` immediately with no network call
  (the catalog requires a live fetch).

A new `verdict discovery refresh [--json] [--dry-run]` subcommand is wired
through `verdict/commands/parsers_discovery.py` and
`verdict/commands/dispatch.py`.

## Capabilities

### New Capabilities

- `discovery-prober`: Per-pool serial liveness and tool-call prober for free and
  subscription OmniRoute routes with injectable transport for offline tests.
- `discovery-registry`: 12-hour proof registry at
  `$VERDICT_HOME/discovery/registry.json`, written atomically, with per-pool
  circuit breaker and `probe_skip_reason` for not-probed entries.

### Modified Capabilities

- `discovery-funnel` (read-only extension): `verdict discovery refresh` adds
  the `refresh` subcommand alongside the existing `funnel` subcommand.  No
  existing funnel behaviour is changed.

## Impact

New package `verdict/discovery/` (7 files, excluding `__init__.py`).  Touched:
`verdict/commands/parsers_discovery.py` (new `refresh` subparser),
`verdict/commands/dispatch.py` (new dispatch branch).  No existing behaviour
changes.  Probe ONLY routes classified FREE or SUBSCRIPTION by a fact; never
probe unknown or priced routes.  All tests are offline (no live provider calls).

## Acceptance criteria

- `verdict discovery refresh` on the live gateway probes OmniRoute routes and
  writes `$VERDICT_HOME/discovery/registry.json` with `answered` and `tool_ok`
  fields populated.
- `verdict discovery refresh --dry-run` returns immediately with
  `source="dry_run_requires_catalog"` and makes no network calls.
- Per-pool circuit breaker: a 429 on route A opens that pool; subsequent routes
  in the same pool appear with `probe_skip_reason="pool_breaker_open"`.

## Evidence boundary

- KNOWN: the prober contract (probe_one, tool_probe, run_refresh, circuit
  breaker, registry schema, dry-run guarantee) was read in this repository and
  is covered by the offline tests added here.  S2's OmniRoute adapter
  (fetch_omniroute_evidence, rows_from_evidence) was read and confirmed to
  produce the evidence fields this change consumes.
- INFERRED: which OmniRoute routes the live gateway classifies as free or
  subscription at run time; that is measured per run, never assumed.
- NOT AVAILABLE: a live end-to-end refresh run on the operator's gateway.  This
  change ships the code path and its hermetic tests; the real-command proof is
  the ticket owner's post-merge step.
