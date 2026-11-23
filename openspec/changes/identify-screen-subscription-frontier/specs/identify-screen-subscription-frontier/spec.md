# Spec Delta

## ADDED Requirements

### Requirement: The tier filter SHALL pass routes with unknown capability tier
When `--max-capability-tier N` is set, the screen SHALL skip only routes whose
capability tier is a known value greater than N.  Routes whose capability tier
is unknown (classifier returns None) SHALL NOT be filtered; they pass to the
probe stage.

#### Scenario: Unknown-tier route passes max_capability_tier filter
- **WHEN** `--max-capability-tier 1` is set and a route has an unknown tier
- **THEN** the route is not given result `skipped_unknown_tier`; it proceeds to the probe stage

#### Scenario: Known-excessive-tier route is still filtered
- **WHEN** `--max-capability-tier 2` is set and a route has known tier 3
- **THEN** the route is given result `skipped_capability_tier` and is not probed

### Requirement: Subscription frontier routes SHALL be eligible for screening
Routes that appear in the inventory (`/v1/models`) but are not in the
OmniRoute auto-combo candidate pool SHALL be included in the unrated-route
screening pool when their route-prefix provider has at least one usable
connection.

#### Scenario: Subscription frontier route is included
- **WHEN** a route is in `inventory_route_ids`, not in `live_candidates`,
  and its route-prefix is in `usable_providers`
- **THEN** the route appears as a screening candidate with `basis=unknown`

#### Scenario: Subscription frontier route with no usable connection is excluded
- **WHEN** a route is in `inventory_route_ids`, not in `live_candidates`,
  and its route-prefix is NOT in `usable_providers`
- **THEN** the route does not appear as a screening candidate

### Requirement: Extra-ratings provider SHALL use the connection namespace
For routes in the auto-combo candidate pool, the `provider` field recorded for
the extra-ratings entry SHALL be `live_candidates[rid].provider` (the
connection namespace), not the `owned_by` field from the route inventory.

#### Scenario: Provider filter uses connection namespace
- **WHEN** a route has `live_candidates.provider = "cc"` and `route_identities` says `"anthropic"`
- **THEN** `--provider cc` includes the route and `--provider anthropic` excludes it

### Requirement: Opaque routes SHALL never be added to the extra-ratings pool
Routes matched by `is_opaque(rid)` or `is_non_chat_route(rid)` SHALL be
excluded from `extra_ratings` regardless of their presence in the inventory or
live candidates.

#### Scenario: Opaque inventory route is never screened
- **WHEN** an opaque route (`auto/best`) is in `inventory_route_ids`
- **THEN** the route does not appear in the screen report
