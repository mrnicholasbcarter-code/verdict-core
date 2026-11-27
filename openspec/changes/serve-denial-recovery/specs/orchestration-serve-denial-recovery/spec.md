# Spec Delta

## ADDED Requirements

### Requirement: A serve route denial is classified as route_denied

When a worker attempt fails with HTTP 503 and its error is `verdict serve`'s denial of the attempt's own assigned route (`assigned route '<id>' denied: <code>`), the failure SHALL be classified `route_denied` with action `REROUTE`, scope `none` and no cooldown, and SHALL carry the denial code. A 5xx failure without that text, or with a denial text that names another route, SHALL be classified as before.

#### Scenario: serve refuses the assigned route

- **WHEN** an attempt on `claude/X` ends with status 503 and the error `assigned route 'claude/X' denied: opaque_route_disallowed`
- **THEN** the failure category is `route_denied` and its evidence names `opaque_route_disallowed`

#### Scenario: upstream server error

- **WHEN** an attempt ends with status 503 and an error that is not serve's denial text for its route
- **THEN** the failure category is `upstream_temporary` with a route-scoped 60-second cooldown

### Requirement: A permanently refused route is excluded for the rest of the run

When the denial code is permanent (`opaque_route_disallowed`, `metadata_ghost`), the run SHALL remove the route from its admitted set, for every node, until the run ends. The admitted set SHALL only narrow. The run SHALL NOT write a health-cache entry or a persisted cooldown for the denial.

#### Scenario: another node would select the refused route

- **WHEN** node `a` was refused on `claude/X` with a permanent code and node `b` later selects a route
- **THEN** `claude/X` is not selected, and node `b`'s selection record shows it excluded with `serve_denied:opaque_route_disallowed`

#### Scenario: nothing outlives the run

- **WHEN** a run ends after a permanent denial
- **THEN** the persisted health state and the health cache hold no entry caused by that denial

### Requirement: A temporary denial cools routes for the run only

When the denial code is `provider_unhealthy`, the run SHALL put every route whose id has the same prefix as the denied route in a cooldown of 300 seconds and SHALL NOT remove any route from the admitted set. For any other non-permanent code the run SHALL cool only the denied route, for 60 seconds. These cooldowns SHALL be held for the run only and SHALL NOT be written to the persisted health state.

#### Scenario: provider reported unhealthy

- **WHEN** an attempt on `cerebras/A` is refused with `provider_unhealthy`
- **THEN** `cerebras/A` and `cerebras/B` are not selected for 300 seconds and are selectable afterwards

#### Scenario: twin identities

- **WHEN** an attempt on `claude/X` is refused with any code
- **THEN** `cc/X` is neither cooled nor removed

#### Scenario: unknown code

- **WHEN** an attempt is refused with a code this version does not name
- **THEN** only the denied route is cooled, for 60 seconds, and the admitted set is unchanged

### Requirement: The run records what it did with a denial

For every `route_denied` failure the run SHALL emit a `controller` event with `phase` `serve_denial`, the route, the denial code and the action taken (`excluded`, `prefix_cooled` or `route_cooled`).

#### Scenario: permanent denial

- **WHEN** a route is excluded after a permanent denial
- **THEN** the event names the route, the code and the action `excluded`
