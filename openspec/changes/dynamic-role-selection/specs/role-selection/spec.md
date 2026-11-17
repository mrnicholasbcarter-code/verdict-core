# Spec Delta

## Purpose

The orchestrator dynamically selects models for different roles (Planner, Implementer, Reviewer, Controller) based on a pipeline of eligibility, capacity, and measured strength.

## ADDED Requirements

### Requirement: Selection Pipeline Contract
The selection SHALL follow a four-stage pipeline:
1. **Inventory (S0)**: Start with live `/v1/models`.
2. **Admission (S1)**: Apply the standard admission ladder. Remove opaque routes. Result is the AdmittedSet (H1).
3. **Narrowing (S2)**: Remove candidates based on Capacity, Quota, Pool, Family, and Measured Demotion. Result is H2.
4. **Ordering (S3)**: Order the remaining candidates by `role_strength`. Result is H3.

The selection SHALL ensure that `H3 ⊆ H2 ⊆ H1`.

#### Scenario: Standard selection succeeds
- **WHEN** a node is dispatched with valid inventory
- **THEN** the pipeline returns H3 as a subset of H1 with routes ordered by role strength

#### Scenario: Empty pipeline fails closed
- **WHEN** no routes survive the narrowing stage
- **THEN** the system returns `FAIL_CLOSED` rather than hanging or defaulting silently

### Requirement: Safety Invariants
- **Strength never admits**: A route MUST pass S1 and S2 before its strength can be considered for ordering in S3.
- **Narrowing only removes**: No stage after S1 SHALL add routes back to the candidate set.
- **Fail Closed**: If H3 is empty, the system SHALL return a `FAIL_CLOSED` status.
- **Unknowns**: Unknown runtimes SHALL NOT be healthy.
- **Opaque routes**: `auto/*` and `combo/*` routes SHALL NOT be candidates.
- **Fixtures**: Fixture metadata SHALL NOT satisfy live proof.
- **Economics**: Pay-per-token (`METERED`) capacity SHALL NOT be the default.

#### Scenario: Strength cannot bypass admission
- **WHEN** a route has the highest strength score but fails the admission ladder
- **THEN** it is excluded from H1 and never appears in H3

#### Scenario: Opaque routes are never admitted
- **WHEN** the inventory contains `auto/best-coding` and `combo/planner`
- **THEN** both are removed in S1 and never appear in the AdmittedSet

### Requirement: Role-Specific Constraints
- **Reviewer Independence**: The selected reviewer MUST be from a different model family than the author.
- **Controller Fence**: The controller role MUST be isolated from worker roles.

#### Scenario: Reviewer excludes author family
- **WHEN** the author was served by the `cc` (Claude) family
- **THEN** `cc/*` routes are excluded from the reviewer candidate set

#### Scenario: Controller is isolated from workers
- **WHEN** a worker node selects a controller model
- **THEN** the controller fence prevents it from being dispatched as a worker

### Requirement: Shadow Mode
When `VERDICT_DYNAMIC_SELECTION=shadow`, the system SHALL:
1. Execute the dynamic pipeline and record the result in a `role_ladder.shadow` event.
2. Dispatch the route selected by the legacy (`off`) path.
3. Record the `agree` status between the two decisions.

#### Scenario: Shadow mode dispatches legacy route
- **WHEN** the mode is set to shadow
- **THEN** the dispatched route matches the legacy selection, and a `role_ladder.shadow` event is written

#### Scenario: Shadow mode records disagreement
- **WHEN** the dynamic and legacy selections choose different routes
- **THEN** the shadow event records `agree: false` and both picks

### Requirement: D7 Rule (Proposed)
**Pending operator confirmation**: For critical roles, an `assumed-FREE` status (absence of evidence) SHALL be treated as `UNKNOWN` rather than `FREE`.

#### Scenario: Assumed FREE treated as UNKNOWN for critical roles
- **WHEN** a route has assumed-FREE status and is being considered for a critical role
- **THEN** it is treated as UNKNOWN and excluded from the candidate set
