## ADDED Requirements

### Requirement: Screens SHALL honour an explicit context and tools floor before probing
`verdict identify --screen` SHALL accept `--min-context-tokens N` with a
positive N, and `--require-tools`. When `--min-context-tokens` is set, a route
whose context window is unknown or below N SHALL be reported as
`skipped_task_floor`. When `--require-tools` is set, a route whose tools
support is not known to be true SHALL be reported as `skipped_task_floor`.
Such a route SHALL NOT be probed and SHALL NOT count toward `--max-routes`,
`--max-per-provider` or `--max-per-pool`. The facts SHALL be the ones the
screen row already reports as `context_window` and `supports_tools`. An
unknown fact SHALL NOT be treated as meeting the floor. Without these options
the screen's order, membership and output SHALL be unchanged.

#### Scenario: Context window below the floor is not probed
- **WHEN** a screen runs with `--min-context-tokens 100000` and a candidate route has a 24,000-token context window
- **THEN** no model request is sent for that route and its result is `skipped_task_floor`

#### Scenario: Unknown context window is not probed
- **WHEN** a screen runs with `--min-context-tokens 100000` and the context window of a candidate route is unknown
- **THEN** no model request is sent for that route and its result is `skipped_task_floor`

#### Scenario: Unknown tools support is not probed
- **WHEN** a screen runs with `--require-tools` and the tools support of a candidate route is unknown
- **THEN** no model request is sent for that route and its result is `skipped_task_floor`

#### Scenario: Skipped routes leave the caps for routes that meet the floor
- **WHEN** a screen with `--max-routes 1` and `--min-context-tokens 100000` meets a 24,000-token route before a 200,000-token route
- **THEN** the 200,000-token route is probed and `routes_checked` is 1

#### Scenario: A screen without the options is unchanged
- **WHEN** a screen runs without `--min-context-tokens`, `--require-tools` and `--max-per-pool`
- **THEN** its JSON report is the same as before this change for the same inventory

### Requirement: Screens SHALL honour an explicit per-pool cap and cover pools breadth-first
`verdict identify --screen` SHALL accept `--max-per-pool N` with a positive N.
The pool of a route SHALL be its backend pool as `provider_catalog.backend_pool`
defines it, so prefixes that share one quota share one count. When the option
is set, the screen SHALL probe at most N routes per pool, in addition to
`--max-per-provider` and `--max-routes`, and SHALL give every pool its first
probe before any pool gets a second, keeping the screen's existing order
inside a pool. A route not probed because its pool is at the cap SHALL be
reported as `skipped_pool_cap`, with `stopped_reason` `pool_cap` when no other
cap was reached. A skipped route SHALL NOT take a pool slot. When the option
is set, every row SHALL report `backend_pool`. The option SHALL NOT add a
candidate or change which routes are admissible.

#### Scenario: Two prefixes on one quota get one probe
- **WHEN** a screen runs with `--max-per-pool 1` and its candidates include an `agy/*` route, an `antigravity/*` route and a route in another pool
- **THEN** one of the two aliased routes is probed, the other is `skipped_pool_cap`, and the route in the other pool is probed

#### Scenario: Pools are covered before any pool is probed twice
- **WHEN** a screen runs with `--max-routes 3` and `--max-per-pool 2` over three pools, the first two holding two routes each
- **THEN** the three probes are the first route of each pool
