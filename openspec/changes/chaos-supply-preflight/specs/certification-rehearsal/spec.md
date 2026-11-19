## ADDED Requirements

### Requirement: The rehearsal SHALL start only with planner-eligible passes in five backend pools
The rehearsal preflight SHALL screen at most 13 FREE routes, at most one per
backend pool, so that it sends at most 39 model requests. It SHALL stop with
`RehearsalConfigurationError` before any orchestration starts when its
planner-eligible passes sit in fewer than 5 distinct backend pools, with a
message that states the number found and the number required. Five is what
the chaos rehearsal consumes: two injected planner faults, a rate-limited
worker pool and its replacement, and an independent reviewer that excludes
implementer routes and their aliased pools.

#### Scenario: Passes in four pools
- **WHEN** the planner-eligible passes in the preflight report sit in 4 backend pools
- **THEN** the rehearsal raises `RehearsalConfigurationError` naming "in 4 backend pool(s); 5 are required" and no orchestrate command runs

#### Scenario: Bounded reach
- **WHEN** the script builds the preflight command
- **THEN** it carries `--capacity free`, `--max-routes 13` and `--max-per-pool 1`

### Requirement: The chaos rehearsal SHALL start from the state the preflight left
Right after the preflight passes and before the clean rehearsal starts, the
script SHALL make private byte copies, mode 0600 and never overwriting, of the
shared health cache and the shared session-evidence ledger (an empty ledger
when the preflight left none). The chaos `verdict orchestrate` command SHALL
get `VERDICT_HEALTH_CACHE` and `VERDICT_SESSION_EVIDENCE` set to the copies.
Nothing the clean rehearsal writes SHALL reach the chaos rehearsal. The clean
rehearsal's command and environment SHALL NOT change, and no record SHALL be
edited.

#### Scenario: A clean-run failure does not score a chaos route
- **WHEN** the clean rehearsal records a session failure and a cooldown in the shared files
- **THEN** the chaos rehearsal's ledger and health cache are byte-identical to the shared files as they stood right after the preflight

#### Scenario: No proof stops both runs
- **WHEN** the shared health cache holds no fresh agentic proof right after the preflight
- **THEN** the script raises `RehearsalConfigurationError` and neither rehearsal runs
