## ADDED Requirements

### Requirement: The rehearsal preflight SHALL probe only routes at the planner floor, one per backend pool
The rehearsal preflight SHALL run its screen with `--min-context-tokens` set
to the frontier planner's minimum context window and with `--require-tools`
when the planner requires tools, both read from the constants
`plan_with_failover` uses. It SHALL pass `--max-per-pool 1`. The capacity
SHALL stay `free`, and `--max-routes` and `--max-per-provider` SHALL stay at
their existing values.

#### Scenario: Preflight argv carries the floor and the pool cap
- **WHEN** the rehearsal builds the preflight command
- **THEN** it contains `--min-context-tokens 100000`, `--require-tools` and `--max-per-pool 1` together with `--capacity free`, `--max-routes 8` and `--max-capability-tier 2`

### Requirement: The rehearsal SHALL start only with planner-eligible passes in three backend pools
The rehearsal preflight SHALL count the distinct backend pools among its
planner-eligible passes, deriving each pool from the route id. It SHALL stop
with `RehearsalConfigurationError` before any orchestration starts when the
count is below 3, with a message that states the number found and the number
required. The number 3 covers a clean run whose workers use one pool and are
reassigned once to a second, leaving a third for the independent reviewer.
What the chaos rehearsal needs has not been measured and MAY be higher.
`certification-failure.json` SHALL carry the count as
`planner_eligible_pools` and the requirement as
`planner_eligible_pools_required`, both as numbers, and SHALL NOT carry a
route id or a pool name.

#### Scenario: Passes in two pools
- **WHEN** the planner-eligible passes in the preflight report sit in 2 backend pools
- **THEN** the rehearsal raises `RehearsalConfigurationError` naming "in 2 backend pool(s); 3 are required" and no orchestrate command runs

#### Scenario: Two passes in one aliased pool
- **WHEN** the planner-eligible passes are an `agy/*` route, an `antigravity/*` route and one route in another pool
- **THEN** the count is 2 and the rehearsal stops before orchestration

#### Scenario: Passes in three pools
- **WHEN** the planner-eligible passes in the preflight report sit in 3 backend pools
- **THEN** the rehearsal continues to orchestration
