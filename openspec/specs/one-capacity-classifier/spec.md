# one-capacity-classifier Specification

## Purpose
Verdict decides a route's capacity class (FREE, SUBSCRIPTION, METERED or UNKNOWN) in exactly one place, `verdict.capacity.capacity_of`, from declared or observed facts. Every surface then agrees on the class, and a route's name never decides it.

## Requirements

### Requirement: Single capacity entry point

All production capacity decisions SHALL go through `verdict.capacity.capacity_of`.
No production caller SHALL call `classify_free` or `connection_signals_free` directly
other than the modules whitelisted in `reachability-forbidden.json`. A `FreeTierModel`
route identifier SHALL be built via its `.route_id` property, which normalises the
provider prefix and never double-prefixes.

#### Scenario: capacity_of is the only production call site
- **WHEN** any production module needs a capacity class for a route
- **THEN** it calls `capacity_of(conn, row, route_id=..., openrouter_free_set=...)` and receives `(CapacityClass, deciding_fact)`

#### Scenario: gateway_free uses normalised route ids
- **WHEN** the intelligence layer builds the gateway free-tier membership set from `snapshot.free_tier`
- **THEN** the set is built as `{row.route_id for row in snapshot.free_tier}` so provider prefixes are never doubled and comparisons against route ids always match

#### Scenario: Forbidden primitives blocked
- **WHEN** a module outside the allowlist imports `classify_free` or `connection_signals_free` directly
- **THEN** `check_reachability.py` reports a forbidden-symbol violation
