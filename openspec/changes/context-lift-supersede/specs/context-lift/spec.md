# Spec Delta

## ADDED Requirements

### Requirement: ingest_lift_fact accepts repeated calls on the same plane

`ingest_lift_fact` MUST succeed when called more than once against the same
`MemoryPlane` with a different token; the second call MUST supersede the first
active record and become the new active record.

#### Scenario: second ingest supersedes the first

- **GIVEN** `ingest_lift_fact(gate, token1)` has been called and returned `record1`
- **WHEN** `ingest_lift_fact(gate, token2)` is called with a different token
- **THEN** the call returns `record2` without raising
- **AND** `record2.supersedes == record1.record_id`
- **AND** `plane.get("patterns", "lift_fact").record_id == record2.record_id`

#### Scenario: first ingest on a fresh plane succeeds unchanged

- **GIVEN** a fresh `MemoryPlane` with no prior `patterns/lift_fact` record
- **WHEN** `ingest_lift_fact(gate, token)` is called
- **THEN** the call returns a `MemoryRecord` without raising
- **AND** `token` appears in the returned record's `content`

### Requirement: non-secret ingest failure uses accurate error code

When `ingest_lift_fact`'s write is refused for a reason other than secret
detection, the raised `ContextIntelligenceError` MUST carry code `ingest_refused`,
not `secret_refused`. Secret-detection failures MUST continue to use `secret_refused`.

#### Scenario: non-secret ingest failure code is ingest_refused

- **GIVEN** a `MemoryGate` whose write is denied for a non-secret policy reason
- **WHEN** `ingest_lift_fact` raises
- **THEN** `exc.code == "ingest_refused"`
