# Spec Delta

## ADDED Requirements

### Requirement: The tier filter SHALL use the planner band for unknown-tier routes
When `--max-capability-tier N` is set, the screen SHALL skip routes whose
capability tier is a known value greater than N with result `skipped_capability_tier`.
Routes whose capability tier is unknown (classifier returns None) SHALL be
filtered with result `skipped_unknown_tier` UNLESS their stripped model key
appears in the ADR-051 planner band, frontier or fallback (the fallback set
includes the frontier set), per fresh `aa_intelligence` ratings.  A no-tier
route whose model is in the band SHALL proceed to the probe stage.  Band
membership SHALL NOT admit the route or count as proof of capability.

#### Scenario: Unknown-tier route in planner band passes the tier filter
- **WHEN** `--max-capability-tier 1` is set and a route has an unknown tier
- **AND** the route's stripped model key is in the ADR-051 fallback band (fresh `aa_intelligence` ratings)
- **THEN** the route is not given result `skipped_unknown_tier`; it proceeds to the probe stage

#### Scenario: Unknown-tier route with stale or missing ratings is filtered
- **WHEN** `--max-capability-tier 1` is set and a route has an unknown tier
- **AND** the route has no fresh `aa_intelligence` rating (stale or absent)
- **THEN** the route is given result `skipped_unknown_tier` and is not probed

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
