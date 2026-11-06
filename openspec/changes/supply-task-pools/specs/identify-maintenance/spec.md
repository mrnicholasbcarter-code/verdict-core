## ADDED Requirements

### Requirement: Passive inventory reads the live candidate pool
Passive identify SHALL read `/v1/auto-combo/auto/candidates` as a seventh source slot, after
the existing six, with the resolved inference API key only, under the same aggregate deadline,
decoded-byte budget and row cap. It SHALL attempt at most seven GETs and perform no POST, write
or model call. A missing inference key SHALL make the candidates slot unavailable without
network access. A candidate row SHALL be usable only when `reachable` is true, `excluded` is
false, `breakerState` is `CLOSED`, `connectionCooldown` is false, `modelLocked` is false and
`freeAccessExclusion` is null; a missing or wrongly typed field SHALL make the row not usable.
A route SHALL be live reachable only when at least one of its rows is usable.

#### Scenario: Seven bounded GETs with split credentials
- **WHEN** passive identify runs with both credentials available
- **THEN** the five `/api` requests carry only the management credential
- **AND** `/v1/models` and `/v1/auto-combo/auto/candidates` carry only the inference API key
- **AND** at most seven GETs are attempted

#### Scenario: Candidates source fails
- **WHEN** the candidates response is not an object with a `candidates` list or the read fails
- **THEN** `candidates` is listed in `source_errors`
- **AND** no route is treated as live reachable

#### Scenario: Odd candidate fields are not healthy
- **WHEN** a candidate row lacks `reachable` or reports a breaker state other than `CLOSED`
- **THEN** that row is not usable and its reason is named

### Requirement: Passive report states configured supply and its unknowns
The passive report SHALL include `supply` with per-provider and total counts of catalog
routes, live candidates, live reachable routes, traffic-proven routes, tool-proven routes and
connections, and capacity counts over live candidates. It SHALL keep the layers separate:
catalog, live, tool proof and task performance; task performance SHALL be `not_measured`.
`supply.complete` SHALL be false unless every completeness check is clean, and
`supply.unknowns` SHALL be a sorted list of named gaps. Account identity SHALL be reported only
as connection ids, never emails.

#### Scenario: Connection without live candidates
- **WHEN** a provider has a configured connection but no row in the candidate pool
- **THEN** `supply.unknowns` names `connections_without_live_candidates:<provider>`
- **AND** `supply.complete` is false

#### Scenario: Candidates source unavailable
- **WHEN** the candidates source has an error
- **THEN** `supply.unknowns` names `candidates_source_unavailable`
- **AND** `supply.complete` is false

#### Scenario: Layers are not merged
- **WHEN** a route is in the catalog and live but has no fresh tool proof
- **THEN** it counts toward the catalog and live layers and not toward the tool-proof layer
- **AND** `supply.layers.task_performance` is `not_measured`

### Requirement: Passive report orders per-task route pools
The passive report SHALL include `task_pools` for `research`, `specification`,
`implementation`, `review` and `integration`. Hard admission SHALL come before ordering: a
route SHALL NOT be opaque (`auto/*`, `combo/*`), SHALL be in the catalog, live reachable, on a provider with a usable connection, not
cooling down, and without a latest traffic status of 401, 403 or 404. An admitted route with
fresh tool proof for itself or an alias of the same backend model SHALL be `eligible`. An
admitted route without fresh tool proof SHALL be `fallback`, which is probation that needs
bounded qualification and SHALL never be dispatch-authorized. Other routes SHALL be counted by
blocking reason only. Eligible and fallback rows SHALL be ordered by capacity (free,
subscription, metered, unknown), then task rating strongest first with unknown last, then route
id. Rows sharing a backend model SHALL collapse to the first row, which lists the others as
`aliases`. The report SHALL keep `dispatch_authorized` false. Passive identify SHALL read tool
proof from the health cache without writing it; an unreadable cache SHALL be reported as
`tool_proof_error: health_cache_unavailable`.

#### Scenario: Free route before metered route
- **WHEN** a free route and a metered route are both eligible for implementation
- **THEN** the free route is listed first regardless of rating

#### Scenario: No fresh tool proof is fallback only
- **WHEN** an admitted route has missing, stale or negative tool proof
- **THEN** it is listed under `fallback`, not `eligible`
- **AND** `task_pools.dispatch_authorized` is false

#### Scenario: Aliases collapse to one row
- **WHEN** two route ids resolve to the same backend model
- **THEN** the pool lists one row and names the other route id in `aliases`

#### Scenario: Opaque routes never enter a pool
- **WHEN** an `auto/*` or `combo/*` route is in the catalog and live candidates, with or without fresh tool proof
- **THEN** it is neither eligible nor fallback
- **AND** it is counted under `opaque_route`

#### Scenario: Unknown liveness is not admitted
- **WHEN** the candidates source failed
- **THEN** no route is eligible or fallback
- **AND** the routes are counted under a liveness blocking reason

### Requirement: Doctor summarizes pools without changing its verdict
On an ok passive report that contains `task_pools` and `supply`, the doctor identify report
row SHALL append the implementation pool eligible count, its free count, the fallback count,
`supply.complete` and the number of unknowns. A report without those keys SHALL keep the
existing text. The summary SHALL NOT change when the row is ok or failed.

#### Scenario: New report
- **WHEN** a fresh, error-free passive report contains `task_pools` and `supply`
- **THEN** the row detail ends with `implementation pool eligible N (free F), fallback M; supply complete false, U unknowns`

#### Scenario: Old saved report
- **WHEN** a fresh, error-free passive report has no `task_pools` key
- **THEN** the row detail is unchanged and the row is ok
