# identify-reads-connections-snapshot design

## Context

The certification gateway key has only the `self:usage` scope. `identify`
previously read connection, quota and cooldown evidence from five management
endpoints that the restricted key cannot reach. Every route's capacity became
`unknown`, `--capacity free` dropped all candidates, and the preflight checked
0 routes.

`orchestrate` already reads a sanitised `VERDICT_CONNECTIONS_SNAPSHOT` supplied
by the CI workflow. Extending `identify` to use the same snapshot removes the
management-endpoint dependency for the certification path.

## Goals / Non-Goals

**Goals:**
- `identify` reads connection evidence from `VERDICT_CONNECTIONS_SNAPSHOT` when
  set, using the same reader and 6-hour freshness rule that `orchestrate` uses.
- The preflight summary reports `source_errors` with source names and HTTP
  status or exception class only — no URL, key or body reaches the artifact.

**Non-Goals:**
- Changing admission, ranking, review, capacity rules or preflight bounds.
- Adding a fallback when the snapshot is absent or invalid; fail closed is
  the correct behaviour.
- Modifying inference reads (`/v1/models`, candidates).

## Decisions

Use `read_connections_snapshot` (the shared reader) and the existing snapshot
path and freshness rule without introducing a separate code path. Reuse the
existing usability and capacity classification rules unchanged.

Restrict `source_errors` to a closed vocabulary of source names and kinds so
no dynamic data from the gateway (URLs, keys, response bodies) can reach the
certification artifact.

## Risks / Trade-offs

Fail-closed behaviour: an invalid, stale or missing snapshot blocks
certification entirely. This is intentional; a stale snapshot is less safe
than a hard failure.

## Security / trust implications

KNOWN: the snapshot is sanitised before it reaches the CI environment.
`source_errors` is enforced to contain only names and kinds; the test asserts
no URL or key is present.

## Routing / context / memory implications

No impact on routing decisions beyond the capacity inference that already ran
from management reads.

## Migration / rollback

Set `VERDICT_CONNECTIONS_SNAPSHOT` in the CI workflow environment to enable the
new path. If unset, `identify` falls back to management reads (existing
behaviour). Rolling back is as simple as unsetting the environment variable.

## ADR impact

No ADR changes required. The snapshot design is established by the
`restricted-connections-snapshot` change (#807).
