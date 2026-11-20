# Spec Delta

## Purpose

When a verification command fails because the environment is broken
(missing runner, missing module, non-executable binary), the failure SHALL
be classified as `environment_error` — not `verification_failed` — and the
orchestrator SHALL fail the node closed with zero route-level impact:
no session ledger write, no health-cache write, no escalation.

## ADDED Requirements

### Requirement: environment_error classification for verification runner failures

When a verification command exits with code 126 or 127, OR when a
`python -m MODULE` command exits with "No module named MODULE" in its
output, the runtime SHALL classify the failure as `environment_error`,
not `verification_failed`.

#### Scenario: exit 127 (command not found) during verify

- **WHEN** the verification runner exits with code 127
- **THEN** the failure category is `"environment_error"`, the node state
  is BLOCKED, and no health-cache or session ledger entry is written for
  the route

#### Scenario: missing module for -m invocation

- **WHEN** `python -m pytest` (or any `-m MODULE` invocation) exits with
  "No module named MODULE" in its output
- **THEN** the failure category is `"environment_error"`, the node state
  is BLOCKED, attempt count is 1, and no health-cache or session ledger
  entry is written for the route

### Requirement: environment_error fails closed with no route impact

For any failure classified as `environment_error`, the runtime SHALL:
- Set `FailureClassification.action = "BLOCK"`,
  `cooldown_seconds = 0`, `scope = "none"`
- NOT increment `judged_failure_count`
- NOT write any session ledger or health-cache entry for the route
- Transition the node to BLOCKED

#### Scenario: environment_error leaves route health unchanged

- **WHEN** a node is classified `environment_error`
- **THEN** `FailureClassification.action == "BLOCK"`,
  `FailureClassification.scope == "none"`,
  `FailureClassification.cooldown_seconds == 0`, and no session
  ledger or health-cache entry is written for the route

### Requirement: pre-dispatch runner check

Before making the first executor call for a node whose verification command
has a runner (argv[0]) that is not on PATH, the runtime SHALL:
- Classify the failure as `environment_error` with `attempt = 0`
- Transition the node directly to BLOCKED
- NOT call the executor

#### Scenario: missing runner blocks at attempt 0

- **WHEN** the node's `verification_command` has argv[0] not resolvable
  via `shutil.which()` and argv[0] is not `"python"` or `"python3"`
- **THEN** the node is BLOCKED, `attempt == 0`, and the executor is never
  called

## UNCHANGED Requirements

`verification_failed` classification for genuine wrong-output failures is
unchanged: when the verification runner runs successfully but the output
does not match expectations (exit non-zero for reasons other than env
failure), the category remains `"verification_failed"` with its existing
`action`, `cooldown_seconds`, and `scope`.
