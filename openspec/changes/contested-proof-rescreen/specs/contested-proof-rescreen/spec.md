# Spec Delta

## ADDED Requirements

### Requirement: Acceptance queues rescreen hints for failing listed routes

After the hourly tool check, `eligibility_check` SHALL call
`HealthCache.request_rescreen` with the route IDs of listed routes whose final
tool-check outcome (after the BOD-345 retry) was `"fail"`. This SHALL be a
scheduling hint only: no health entry, proof record, or capability decision is
written. A cache error during hint queuing SHALL NOT fail the acceptance run.

#### Scenario: All-fail listed routes produce a rescreen hint

- **GIVEN** a listed route whose first and second tool-check attempts both return `"fail"`
- **WHEN** `eligibility_check` completes
- **THEN** `HealthCache.rescreen_requests()` contains that route ID
- **AND** the route's health entry is unchanged

#### Scenario: Partial failure does not produce a rescreen hint

- **GIVEN** a listed route whose first attempt returns `"fail"` and second attempt returns `"pass"`
- **WHEN** `eligibility_check` completes
- **THEN** `HealthCache.rescreen_requests()` does NOT contain that route ID

### Requirement: Identify screen re-probes a fresh route with a pending rescreen request

`screen_models` SHALL bypass the `skipped_fresh` guard for a route that has a
pending rescreen request in `HealthCache.rescreen_requests()`. The route SHALL
be probed even if its agentic proof is fresh. It SHALL still respect the
negative-state check, cooldown, route cap, provider cap, pool cap, and the
shared lease. `HealthCache.complete_rescreen` SHALL be called for the route
after the probe sends its first turn.

#### Scenario: Requested fresh route is probed

- **GIVEN** a route with agentic proof younger than 24 hours
- **AND** that route is in `HealthCache.rescreen_requests()`
- **WHEN** `screen_models` runs
- **THEN** the route is probed (not `skipped_fresh`) and its rescreen hint is consumed

#### Scenario: Unrequested fresh route is still skipped

- **GIVEN** a route with agentic proof younger than 24 hours
- **AND** that route is NOT in `HealthCache.rescreen_requests()`
- **WHEN** `screen_models` runs
- **THEN** the route result is `skipped_fresh`

### Requirement: Named reprobe budget is reserved before the chat phase

When `Prober.run_once()` is called with a non-empty `reprobe_routes`, it SHALL
compute `_reprobe_reserved = min(admitted_free_reprobe_count * AGENTIC_TURNS,
max_requests)` before the chat phase runs. The chat coordinator SHALL receive
`max_requests - _reprobe_reserved` as its budget. When that reduced budget is
zero or less, the chat phase SHALL return early without probing. The named
reprobe routes SHALL still run on the remaining budget.

#### Scenario: Reprobe gets its turns despite chat using its share

- **GIVEN** `max_requests=6`, one admitted FREE reprobe route (3 turns reserved)
- **AND** the chat phase is configured to consume all requests it receives
- **WHEN** `run_once()` completes
- **THEN** the named reprobe route was probed
- **AND** `stats.requests <= 6`
