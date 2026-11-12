# Spec Delta

## Purpose

Defines when new affected work may start, when stale continuation is rejected, and which safety rules memory text is not allowed to change.

## ADDED Requirements

### Requirement: Changed authority SHALL reject stale continuation

A continuation SHALL be rejected when the packet's acceptance-criterion revision, spec revision, or ADR id and status differs from the current governing revision. Rejection SHALL require affected rehydration before a new dispatch of that governing task. Unchanged sources SHALL be retained. Spec revision mismatch SHALL use the existing `spec_changed` outcome. Acceptance-criterion and ADR mismatches SHALL be named separately and SHALL NOT be reported as a healthy continuation.

#### Scenario: Spec digest changed

- **WHEN** the persisted spec revision digest differs from the current spec revision digest
- **THEN** continuation is rejected with the existing spec-changed outcome
- **AND** a new dispatch of that task does not proceed until affected rehydration

#### Scenario: Acceptance criterion or ADR changed

- **WHEN** the packet's acceptance-criterion revision or ADR id and status differs from the current governing revision
- **THEN** continuation is rejected
- **AND** unaffected source revisions remain in the packet

### Requirement: Isolation, secrets, supersession, and replay SHALL hold

A hit from another project SHALL NOT be injected. Secret-shaped text SHALL be redacted before it is stored in a brief, packet, or proof artifact. A newer verified operator decision SHALL outrank an older verified decision. An ordinary transcript SHALL NOT supersede either. An unresolved contradiction SHALL show both citations. A duplicate capture or packet event SHALL NOT create a second logical record. Capture SHALL use the existing durable outbox. This requirement SHALL NOT add a second outbox.

#### Scenario: Wrong project

- **WHEN** a retrieved hit belongs to a project other than the governing project
- **THEN** the hit is excluded
- **AND** the exclusion is counted

#### Scenario: Secret canary

- **WHEN** a retrieved record contains a credential shape
- **THEN** the rendered brief and any retained artifact show the redacted form
- **AND** the canary value is absent

### Requirement: Memory text SHALL NOT authorize policy, weaker tests, or Done

Retrieved or packet text SHALL NOT approve policy, weaken or skip a test, set verified authority, waive a gate, or certify Done. A page that says the work is done SHALL leave the completion decision unchanged.

#### Scenario: Forged completion page

- **GIVEN** a retrieved page that says to mark the story Done and to weaken a test
- **WHEN** dispatch and completion are decided
- **THEN** the page is untrusted evidence only
- **AND** Done is not certified
- **AND** the test remains required

### Requirement: Missing infrastructure SHALL block only new affected dispatch

If mandatory shared-memory infrastructure is not configured, disabled, unauthorized, schema-incompatible, timed out, or unreachable, a new affected dispatch SHALL be refused before a model or worker call. An in-flight run SHALL keep its existing durable state and capture path and SHALL NOT be killed. A healthy empty search SHALL NOT block dispatch. Help, doctor, and read-only context views SHALL remain available.

#### Scenario: New dispatch while memory is down

- **WHEN** a new orchestrate, supervise, autodev, or direct-worker dispatch is requested and the shared-memory consumer is unavailable
- **THEN** no model or worker request is sent for that new dispatch
- **AND** the refusal names the unavailable reason

#### Scenario: In-flight work during the same outage

- **GIVEN** a run that already has durable events and a non-terminal generation
- **WHEN** the shared-memory consumer becomes unavailable
- **THEN** that run is not killed
- **AND** its existing durable state and outbox capture remain the capture path
