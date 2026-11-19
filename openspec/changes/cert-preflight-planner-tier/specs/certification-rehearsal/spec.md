## ADDED Requirements

### Requirement: The rehearsal preflight SHALL qualify only routes the frontier planner can admit
The rehearsal preflight SHALL run its screen with `--max-capability-tier` set
to the frontier planner's tier ceiling, read from the same constant
`plan_with_failover` uses. It SHALL continue to orchestration only when at
least one screen PASS has a known tier within that ceiling, tool support, and
a context window of at least the planner's minimum, also read from shared
constants. Otherwise it SHALL stop with `RehearsalConfigurationError` naming
"no planner-eligible FREE routes (tier<=2, tools, 100k context)" before any
orchestration starts.

#### Scenario: Only tier-3 passes
- **WHEN** every PASS in the preflight report is capability tier 3
- **THEN** the rehearsal raises `RehearsalConfigurationError` naming "no planner-eligible FREE routes" and no orchestrate command runs

#### Scenario: Preflight argv carries the planner ceiling
- **WHEN** the rehearsal builds the preflight command
- **THEN** it contains `--max-capability-tier 2` together with `--capacity free` and the existing route caps

### Requirement: The failure artifact SHALL classify reasons into a closed vocabulary
`certification-failure.json` SHALL report `run_finished_reason` as one of
`planning_none_eligible`, `gateway_auth`, `gateway_discovery`,
`operator_policy`, `harness_inventory_unavailable`, `startup` or
`unclassified`, chosen by fixed substrings of the reason or the `startup`
phase. It SHALL include a `preflight` object holding only `routes_checked`,
`passed`, `planner_eligible_passed` and `metadata_error` (an enum or null).
It SHALL NOT contain raw reason text, child output, URLs, keys or host paths.

#### Scenario: Planning finds no route
- **WHEN** the clean orchestration ends with "planning failed on every eligible frontier model"
- **THEN** the artifact's `run_finished_reason` is `planning_none_eligible`

#### Scenario: Reason text carries a credential
- **WHEN** a reason or preflight field contains the gateway URL or key
- **THEN** neither value appears in the artifact or the printed error
