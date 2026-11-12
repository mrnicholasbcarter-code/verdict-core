## MODIFIED Requirements

### Requirement: Acceptance listed-route independent check retries once

The independent 2-turn tool check (`verdict.acceptance_toolcheck.measure`) probes each route in
the listed sample once. When a listed route's first attempt returns `fail` (anything except an
account-level block), the route is probed a second time with the same request before it counts
against precision. The route counts as failed only when both attempts fail; if either attempt
passes, the route passes. Both attempts are recorded in `result["checks"]`, tagged `attempt: 1`
and `attempt: 2`, so flaky routes stay visible. Account-level outcomes (`401/402/403/429`) are
not retried, and the unlisted recall sample keeps exactly one attempt.

`PRECISION_MIN`, `RECALL_MIN`, `LISTED_SAMPLE` and `UNLISTED_SAMPLE` are unchanged, and the
precision formula (`passes / measured x unblocked share`) is unchanged: the retry changes the
per-route outcome, not the denominator.

#### Scenario: Listed route fails turn 1, passes retry

- **WHEN** a listed route returns HTTP 502 on turn 1 of its independent check and passes turn 2 on the retry
- **THEN** `result["checks"]` holds two entries for that route, `attempt: 1` with `outcome: "fail"` and `attempt: 2` with `outcome: "pass"`
- **AND** the route counts as passing toward listed precision

#### Scenario: Listed route fails both attempts

- **WHEN** a listed route returns HTTP 502 on both attempts
- **THEN** `result["checks"]` holds two entries for that route, both `outcome: "fail"`
- **AND** the route counts as failing toward listed precision exactly once

#### Scenario: Account-blocked listed route is not retried

- **WHEN** a listed route returns HTTP 403 on turn 1
- **THEN** `result["checks"]` holds exactly one entry for that route with `outcome: "account"` and `attempt: 1`
- **AND** no second attempt is made

#### Scenario: Unlisted sample keeps one attempt

- **WHEN** an unlisted sampled route returns HTTP 502
- **THEN** `result["checks"]` holds exactly one entry for that route with `attempt: 1` and `outcome: "fail"`
