# Spec Delta

## ADDED Requirements

### Requirement: Research reuse preserves source-bound identity

The system SHALL bind research identity to the task, observed repository revision and external source revisions, and SHALL append failure or review deltas without changing that identity.

#### Scenario: Retry appends a delta

- GIVEN an unchanged task and source revisions
- WHEN a failure delta is reused
- THEN the research identity remains unchanged and retrieval is not repeated
