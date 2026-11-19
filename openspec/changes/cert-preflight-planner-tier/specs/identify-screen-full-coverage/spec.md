## ADDED Requirements

### Requirement: Screens SHALL honour an explicit capability-tier ceiling before probing
`verdict identify --screen` SHALL accept `--max-capability-tier N` with N from 0
to 3. When it is set, a route whose capability tier is unknown SHALL be
reported as `skipped_unknown_tier`, and a route whose tier is above N SHALL be
reported as `skipped_capability_tier`. Neither SHALL be probed, and neither
SHALL count toward `--max-routes` or `--max-per-provider`. The tier SHALL be
computed by the same rule the eligibility ladder uses. Unknown tier SHALL NOT
be treated as within the ceiling. Every screen row SHALL report
`capability_tier`, `supports_tools` and `context_window`, with `null` for unknown.

#### Scenario: Tier above the ceiling is not probed
- **WHEN** a screen runs with `--max-capability-tier 2` and a candidate route is tier 3
- **THEN** no model request is sent for that route and its result is `skipped_capability_tier`

#### Scenario: Unknown tier is not probed
- **WHEN** a screen runs with `--max-capability-tier 2` and the tier of a candidate route is unknown
- **THEN** no model request is sent for that route and its result is `skipped_unknown_tier`

#### Scenario: Skipped routes leave the caps for eligible routes
- **WHEN** a screen with `--max-routes 1` and `--max-capability-tier 2` meets a tier-3 route before a tier-1 route
- **THEN** the tier-1 route is probed and `routes_checked` is 1
