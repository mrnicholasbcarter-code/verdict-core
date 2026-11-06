# Configured supply and per-task route pools

## Why

Passive identify reports 168 "available" routes, but that list only reflects historical chat
traffic. It does not show what OmniRoute can route right now, which configured connections
supply nothing, or which routes have fresh tool proof. Operators and dispatchers cannot tell
catalog presence, live reachability, tool proof and task performance apart, and there is no
ordered pool of routes per task type.

## What Changes

- Passive identify reads a seventh bounded GET, `/v1/auto-combo/auto/candidates`, with the
  inference key (the same credential rule as `/v1/models`). Each candidate row is usable only
  when OmniRoute reports it reachable, not excluded, breaker `CLOSED`, no connection cooldown,
  no model lockout and no free-access exclusion. A missing or odd-typed field makes the row not
  usable. A failed read is a named source error, not an empty healthy pool.
- The passive report gains `supply`: per-provider and total counts for catalog routes, live
  candidates, live reachable routes, traffic proof and tool proof, plus capacity counts. It
  keeps the layers apart (`catalog`, `live`, `tool_proof`, `task_performance`) and reports
  `complete` together with a sorted list of named `unknowns`. Task performance is reported as
  `not_measured`.
- The passive report gains `task_pools` for research, specification, implementation, review and
  integration. Hard admission comes first (catalog, live, usable connection, not cooled, no
  401/403/404 traffic). Admitted routes with fresh tool proof are `eligible`; admitted routes
  without it are `fallback` (probation that needs bounded qualification, never
  dispatch-authorized). Each pool is ordered free, subscription, metered, unknown, then by
  task rating, then route id. Aliases of the same backend model collapse to one row.
- Passive identify reads tool proof from the health cache read-only. An unreadable cache gives
  `tool_proof_error: health_cache_unavailable`; it never writes.
- The doctor identify report row adds the implementation pool and supply summary when those
  keys exist. Older saved reports keep their text. The ok/failed rules do not change.
- The report keeps `dispatch_authorized: false`. No route is admitted by these pools.

## Capabilities

### Modified Capabilities
- `identify-maintenance`: passive identify reads the live candidate pool and reports supply
  and per-task pools.

## Impact

`verdict/omniroute_evidence.py`, `verdict/identify.py`, new `verdict/identify_pools.py`,
`verdict/identify_service.py`, tests, and the identify section of `docs/CLI_REFERENCE.md`.
No new probe loop, timer, dependency, secret file or numeric policy. No POST, write or model
call. The six-GET requirement in the active `identify-maintenance` change is updated in place to seven
GETs (that change is not archived, so there is no canonical spec to modify).
