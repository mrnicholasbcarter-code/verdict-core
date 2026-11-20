# Design: Discovery prober and registry

## Context

Before this change, Verdict had no native prober.  Operators ran a separate
freescan scan and manually cross-referenced results.  `verdict/discovery/` was
added by an earlier slice but contained only the read-only funnel command (S0).
S2 (`#1180`) added the OmniRoute source (`verdict/discovery/sources/omniroute.py`
with `fetch_omniroute_evidence` and `rows_from_evidence`) and the capacity-class
facts that classify routes as FREE, SUBSCRIPTION, METERED or UNKNOWN.

This change (S3) adds the prober, the proof registry, and the CLI action
(`verdict discovery refresh`).  It removes `verdict/discovery/omniroute_source.py`
(a duplicate fetcher with no producer for its sources-cache.json input) and
points `refresh.py` at S2's adapter.

## Goals / Non-Goals

**Goals:**

- `verdict discovery refresh` probes FREE and SUBSCRIPTION OmniRoute routes and
  writes `$VERDICT_HOME/discovery/registry.json` with 12-hour TTL.
- Per-pool circuit breaker: a 429 on route A opens that pool; subsequent routes
  in the same pool are skipped with `probe_skip_reason="pool_breaker_open"`.
- `probe_skip_reason` distinguishes not-probed routes from probed-but-down routes.
- `--dry-run` returns `source="dry_run_requires_catalog"` immediately with zero
  network calls; the OmniRoute catalog requires a live fetch.
- All tests are offline (injectable transport); no live provider calls in CI.

**Non-Goals:**

- Non-OmniRoute catalogs (openrouter, kilo, models.dev, community) are not
  wired in this story.
- No admission, eligibility or certification change; registry entries are
  advisory only.
- No schema migration for existing registries (new file, not an upgrade).

## Decisions

**Decision 1: single OmniRoute source.**
Use S2's `fetch_omniroute_evidence` + `rows_from_evidence` adapter.  The earlier
`omniroute_source.py` read a `sources-cache.json` file that nothing writes
(S2 writes per-source files at a different path with a different schema), so it
was a dead input.  It is removed; `load_probeable_routes` calls
`_load_omniroute_routes` which calls the S2 adapter directly.

**Decision 2: dry-run returns `dry_run_requires_catalog`.**
With no offline route list (the catalog requires a live fetch), dry-run cannot
report the routes it would probe.  Rather than a misleading empty result, it
returns `source="dry_run_requires_catalog"` immediately.  Zero network calls are
guaranteed by the early-return branch before any `load_probeable_routes` call.

**Decision 3: serial within pools, concurrent across pools.**
Both the liveness stage and the tool-probe stage use `ThreadPoolExecutor` across
pools and serial probing within each pool.  This bounds the 429 blast radius to
one pool while allowing concurrent inter-pool probing.  Rate-limited providers
(`ovhcloud`, `kilocode`, `kilo-gateway`, `pollinations`, `llm7`) get
`max_parallel=1` to avoid tripping their rate limits.

**Decision 4: `probe_skip_reason` as a nullable field on `RegistryEntry`.**
`tool_ok: bool | None` is tri-state (True=proved, False=measured fail,
None=not probed) and was already present.  `probe_skip_reason: str | None`
adds the symmetric field for the liveness stage: `None` means "probed" (not
necessarily passed); a string value names why the probe was skipped
(`"pool_breaker_open"`, `"wall_budget_exhausted"`).

## Risks / Trade-offs

- Route classification depends on the management endpoint.  If the management
  token is absent, `route_capacities` falls back to inventory evidence only;
  some routes may be classified UNKNOWN instead of FREE and go unprobed.
- A route that is FREE by name but not by fact is excluded from probing (the
  capacity facts rule).  This is the intended behaviour per the non-negotiable
  invariant "A route is FREE only from declared or observed facts, never from
  its name."
- The 12-hour TTL makes the registry go stale on a fleet of rapidly rotating
  free routes; a shorter TTL is a follow-up trade-off.

## Security / trust implications

The API key for OmniRoute is resolved from environment variables only
(`OMNIROUTE_API_KEY`, `LLMGATE_API_KEY`, `VERDICT_OMNIROUTE_API_KEY`,
`OMNIROUTE_ACCESS_TOKEN`).  It is never stored to disk or printed.  For routes
with a `literal:xxx` credential, `RegistryEntry.auth` is stored as `"literal"`
(the prefix only), so the secret never appears in the registry file or in
`ProbeResult.detail`.  Tests confirm this sanitisation path.

## Routing / context / memory implications

Registry entries are advisory only: `run_refresh` writes the registry and
`load_registry` reads it, but no admission, eligibility or routing gate is
changed in this story.  The registry is a local file at
`$VERDICT_HOME/discovery/registry.json`; it is never uploaded, shared, or used
to override hard eligibility rules.  No context or memory store is touched.

## Migration Plan

Additive.  `verdict/discovery/omniroute_source.py` is removed but nothing read
it at run time — it had no caller outside deleted tests.  Rollback is reverting
this branch; the registry file (if written) can be deleted without consequence
because nothing reads it yet.

## ADR impact

None.  The capacity-facts rule ("A route is FREE only from declared or observed
facts, never from its name") is an existing invariant enforced by
`tests/test_serve_capacity_order.py::test_route_name_never_makes_a_route_free`.
This change follows that invariant exactly and requires no new ADR.
