## ADDED Requirements

### Requirement: Failed receipt verification leads the run-receipt output

`verdict run-receipt` SHALL, when receipt verification reports any problem, print as its first
line `INTEGRITY FAILED:` followed by the problems, and SHALL show the recorded outcome only on a
later line labelled `recorded (unverified)`. With `--json` it SHALL report `verified: false`,
`outcome: INTEGRITY_FAILED` and the recorded outcome as `recorded_outcome`. It SHALL exit 1.
A receipt that verifies SHALL print its outcome first and `verified: true` in JSON.

#### Scenario: Tampered events log

- **WHEN** `events.jsonl` of a COMPLETE run changed after its receipt was written
- **THEN** the first line starts with `INTEGRITY FAILED:` and does not contain `COMPLETE`
- **AND** a later line starts with `recorded (unverified): COMPLETE`
- **AND** the exit status is 1

#### Scenario: Tampered events log with --json

- **WHEN** the same receipt is shown with `--json`
- **THEN** stdout has `verified` false, `outcome` `INTEGRITY_FAILED` and `recorded_outcome` `COMPLETE`

#### Scenario: Untampered receipt

- **WHEN** the receipt verifies
- **THEN** the first line is the outcome and the second is `integrity: OK (events digest verified)`

### Requirement: route --json prints one JSON document

`verdict route --json` SHALL write exactly one JSON document to stdout and SHALL NOT write a
spinner or the human decision card there. It SHALL exit 0 when a route decision is returned,
including an `--allow-offline` decision that was not executed, and SHALL exit non-zero with a
JSON object that has an `error` field when no route decision is returned.

#### Scenario: Offline decision

- **WHEN** `verdict route --allow-offline --json` returns a decision
- **THEN** stdout parses with `json.loads`, names the model, and the exit status is 0

#### Scenario: Denied decision

- **WHEN** the decision is denied
- **THEN** stdout parses with `json.loads`, has an `error` field, and the exit status is 1

### Requirement: orchestrate --plain always prints the plain narrative

`verdict orchestrate --plain`, and `verdict orchestrate` with `VERDICT_PLAIN=1`, SHALL show the
ASCII progress narrative and SHALL NOT start the coloured live view, also when stdout is a
terminal or pseudo-terminal.

#### Scenario: Pseudo-terminal with --plain

- **WHEN** stdout is a pseudo-terminal and `--plain` is given
- **THEN** the progress view is the plain narrative

#### Scenario: Pseudo-terminal with VERDICT_PLAIN=1

- **WHEN** stdout is a pseudo-terminal and `VERDICT_PLAIN=1` is set
- **THEN** the progress view is the plain narrative
