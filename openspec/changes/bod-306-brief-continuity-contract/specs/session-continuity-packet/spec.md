# Spec Delta

## Purpose

Defines the continuity packet that keeps one governing task's research, proof, and next action across interruption, replacement, and root loss without repeating unchanged work.

## ADDED Requirements

### Requirement: Continuity packets SHALL retain the task snapshot

A continuity packet SHALL record the governing task id, a monotonic packet revision, incremental research with source refs, changed files, last proof, and a non-empty exact next action. Last proof SHALL be either a command, exit, and artifact reference, or an explicit none. Unchanged research SHALL be kept by finding id. A duplicate event id SHALL NOT create another packet revision or another copy of the same finding.

#### Scenario: Interruption after research

- **WHEN** research has been recorded and the writer is interrupted before a final reply
- **THEN** a later read returns the same research, changed files, last proof, and exact next action
- **AND** the governing task id is unchanged

#### Scenario: Duplicate event

- **WHEN** the same event id is applied twice
- **THEN** packet revision and research findings stay at the first application

### Requirement: Replacement SHALL fence the old writer first

A replacement writer id SHALL NOT be recorded until the previous writer id and fence time are recorded. Resume SHALL keep the same governing task id. Resume SHALL accept a different route only when that route is the current eligible-route authorization supplied by existing admission. The packet SHALL NOT select a route and SHALL NOT invent a deadline exception that skips capable free candidates.

#### Scenario: Replacement before fence

- **WHEN** a replacement writer id is recorded and no fence for the previous writer exists
- **THEN** the replacement is rejected

#### Scenario: Resume on another admitted route

- **GIVEN** a fenced previous writer and a currently admitted route that differs from the previous route
- **WHEN** resume is requested for the same governing task
- **THEN** the packet keeps the governing task id
- **AND** records the new admitted route id
- **AND** does not rewrite unchanged research

### Requirement: Refresh SHALL touch only changed or missing sources

On resume, a source whose revision is unchanged SHALL be retained and SHALL NOT be fetched again. A source whose revision changed, or that is missing, SHALL be marked for refresh. Root loss SHALL round-trip the packet from its durable record without changing retained research.

#### Scenario: Unchanged source

- **WHEN** resume compares source revisions and one source revision matches the packet
- **THEN** that source id is retained
- **AND** it is not in the refresh set

#### Scenario: Root restart

- **WHEN** the process that wrote the packet is gone and the packet record is read back
- **THEN** governing task, research, last proof, and exact next action match the last successful write
