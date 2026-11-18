# Spec Delta: Worker idle-output stall kill

## Purpose

Bound how long a silent worker can hold an attempt, and move the node to a
different route when it goes silent.

## ADDED Requirements

### Requirement: A silent worker SHALL be killed as stalled

The Prime headless executor SHALL terminate a worker whose stdout and stderr
produce no bytes for N seconds, where N is `VERDICT_WORKER_IDLE_SECONDS` when
it is a positive finite number and 300 otherwise. Termination SHALL signal the
worker's whole process group with SIGTERM and then SIGKILL after the grace
period. The attempt's terminal SHALL carry `stop_reason` `stalled` and an
error starting with `stalled:`.

#### Scenario: Worker prints one line and sleeps

- **GIVEN** a worker that prints one line and then sleeps for 600 seconds
- **WHEN** the executor runs it with an idle bound of N seconds
- **THEN** the worker and its process group are killed within N + 5 seconds
- **AND** the terminal is not ok and its error starts with `stalled:`

#### Scenario: Malformed setting

- **GIVEN** `VERDICT_WORKER_IDLE_SECONDS` is empty, zero, negative or not a number
- **WHEN** the executor reads its idle bound
- **THEN** the bound is 300 seconds

### Requirement: A stalled attempt SHALL be rerouted within the attempt budget

The failure classifier SHALL classify a stalled terminal as category
`stalled` with action `REROUTE` and route scope. The runtime SHALL then
dispatch the node's next attempt to a route other than the stalled one,
within `max_attempts_per_node`, and SHALL fail closed when the budget is
spent. The receipt SHALL record `failure_category` `stalled` for the attempt.

#### Scenario: Second attempt on a different route

- **GIVEN** a one-node run whose first route stalls and whose pool has another route
- **WHEN** the run finishes
- **THEN** attempt 1 has `failure_category` `stalled`
- **AND** attempt 2 has a different `route_id` from attempt 1
