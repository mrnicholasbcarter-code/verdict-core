# Spec Delta

## Purpose

Defines the cited memory record that a compiled planner or worker brief must carry, including source revisions and the difference between a healthy empty search and an unavailable memory service.

## ADDED Requirements

### Requirement: Compiled briefs SHALL carry one explicit memory status

A compiled planner brief and every new affected worker brief SHALL include a memory status that is exactly one of `cited`, `empty`, or `unavailable`. The status SHALL NOT be omitted. `empty` SHALL mean the authenticated shared-memory consumer returned a healthy search with zero admitted hits. `unavailable` SHALL mean the consumer was not configured, disabled, unauthorized, schema-incompatible, timed out, or unreachable. Those two results SHALL NOT be substituted for each other.

#### Scenario: Healthy search with no admitted hits

- **WHEN** the authenticated shared-memory consumer returns a healthy zero-hit result
- **THEN** the brief status is `empty`
- **AND** decisions and lessons are empty
- **AND** the brief does not say the service was missing or unauthorized

#### Scenario: Auth or infrastructure failure

- **WHEN** the consumer is not configured, disabled, unauthorized, schema-incompatible, timed out, or unreachable
- **THEN** the brief status is `unavailable`
- **AND** the unavailable reason names that class
- **AND** the brief does not report `empty`

### Requirement: Cited briefs SHALL name decisions, lessons, and source revisions

When status is `cited`, the brief SHALL include task, project, and session scope. Session identity SHALL be the known session id or the explicit value `unknown`. Each decision and lesson SHALL cite source id, revision, content hash, project, and observed time. A lesson SHALL be marked derived and SHALL name the source outcome it came from. A decision SHALL NOT be labeled as a lesson. Source revisions SHALL be listed even when the rendered text is trimmed. Omission for budget, sensitivity, supersession, wrong project, or unresolved contradiction SHALL be recorded and SHALL NOT change a `cited` status into `empty`.

#### Scenario: Matching decision and derived lesson

- **GIVEN** a task-scoped decision and a derived lesson that cites that decision's source id
- **WHEN** the brief is compiled for that task and project
- **THEN** both appear with source id, revision, and content hash
- **AND** only the lesson is marked derived

#### Scenario: Session is not known

- **WHEN** no session id is available for an otherwise successful retrieval
- **THEN** session identity is `unknown`
- **AND** status remains `cited` if at least one admitted citation remains

### Requirement: Local plane facts SHALL NOT satisfy the shared-memory status

Gated local MemoryPlane facts MAY remain in the brief as a separately labeled source. They SHALL NOT set the shared-memory status to `cited` or `empty`, and they SHALL NOT hide `unavailable`. The brief SHALL NOT write memory, create a memory database, or construct a second memory client.

#### Scenario: Local facts exist while shared memory is down

- **GIVEN** a gated local fact and an unreachable shared-memory consumer
- **WHEN** the brief is compiled
- **THEN** status is `unavailable`
- **AND** the local fact, if shown, is labeled as local and not as the shared citation
