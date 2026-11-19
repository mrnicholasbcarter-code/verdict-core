## Why

Today there is no single command that shows how many free routes survive each
stage of Verdict's discovery pipeline.  Operators who observe that free models
are not being used have no read-only tool to see where the pipeline is losing
routes.  This change adds `verdict discovery funnel [--json]`, which reads
existing cached evidence and prints every stage with counts and named drop
reasons.

## What Changes

A new CLI sub-command `verdict discovery funnel` reads:
- `~/.verdict/identify-latest.json` for listed and capacity data,
- `~/.verdict/health-cache.json` for probed, answered and tool_ok counts,
- `~/.verdict/admission-latest.json` for the admitted set.

It makes no model calls, no probes and no writes.  Cap constants are named
and exported from their source modules; numbers are never hard-coded in output.
Stages whose evidence is absent report "unknown".

## Capabilities

### New Capabilities
- `discovery-funnel`: Read-only CLI report of the model-discovery pipeline.

### Modified Capabilities
<!-- none: existing admission, health-cache and identify logic is unchanged -->

## Impact

New files: `verdict/discovery_funnel.py`, `verdict/commands/parsers_discovery.py`,
`schemas/discovery-funnel.v1.json`.  Touched: `verdict/cli.py` (import +
direct call), `verdict/commands/dispatch.py` (dispatch branch).  No existing
behaviour changes.
