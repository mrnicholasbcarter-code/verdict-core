# Spec Delta

## Purpose

Defines the cross-component behavior required for Verdict V1 to admit, select, execute, independently check, and learn from model routes using scoped evidence.

## ADDED Requirements

### Requirement: Model control preserves evidence scope and unknowns

The system SHALL retain capacity classes, opaque identities, source scope, timestamps, and completeness when reading inventory and telemetry. Missing, malformed, stale, unsupported, or conflicting facts SHALL remain unknown or unavailable and SHALL NOT create healthy evidence. Public eligibility SHALL require current positive evidence within its applicable authority scope; catalog reads SHALL NOT invoke model calls.

#### Scenario: Incomplete or unsupported inventory
- **WHEN** an inventory source is incomplete, malformed, over its declared bounds, or reports unsupported telemetry
- **THEN** the system SHALL NOT publish it as complete or treat missing facts as healthy, and SHALL preserve valid last-good facts only within their original deadlines

### Requirement: Selection uses admitted task fit without price preference

The system SHALL apply server-owned task, access, capability, and explicit hard budget constraints before ranking. Among admitted candidates, selection SHALL follow trusted task-performance and fit evidence with deterministic ties, independent of price or billing-class labels. Missing price SHALL remain unknown and SHALL NOT reject a candidate unless an explicit hard spend constraint requires a proven bound.

#### Scenario: Price changes without authority changes
- **WHEN** only candidate prices or billing-class labels change while authorization, evidence, task requirements, and explicit budget constraints remain fixed
- **THEN** the admitted set and selected candidate SHALL remain unchanged

### Requirement: Dispatch is scoped, bounded, and revocation-aware

Every Core-controlled send or worker handoff SHALL use a unique, unexpired authorization bound to the gateway, configuration, profile, route, and evidence epoch. A revocation before permit commitment SHALL prevent dispatch; a revocation after commitment SHALL be recorded as possibly sent and SHALL prevent unsafe retry. Qualification producers SHALL use one shared coordinator and aggregate budgets; unknown topology SHALL occupy an exclusive Core lane. Callers joining work SHALL receive an explicit bounded result, and no call SHALL exceed the declared request, output, concurrency, or deadline limits.

#### Scenario: Revocation races with dispatch
- **WHEN** a scoped denial arrives before permit commitment
- **THEN** no transport or worker handoff SHALL occur
- **WHEN** the denial arrives after commitment
- **THEN** the attempt SHALL remain classified as possibly sent and SHALL NOT be retried under that permit

### Requirement: Execution outcomes preserve protocol and identity evidence

The system SHALL distinguish transport completion, protocol completion, verified route identity, and independently checked task success. Stream parsing SHALL preserve protocol terminal states across arbitrary chunk boundaries and enforce accumulated event and total stream bounds. EOF without a valid terminal SHALL be incomplete. Closing a validation or evidence wrapper SHALL close its transport exactly once and record at most one terminal attempt outcome. Missing or mismatching route identity SHALL NOT promote qualification or protected execution.

#### Scenario: Stream ends without a valid terminal
- **WHEN** a stream terminates abruptly, reports failure or incompletion, or contains invalid terminal ordering
- **THEN** the attempt SHALL remain failed or incomplete and SHALL NOT be recorded as checked success

### Requirement: Feedback uses verified task-specific checked outcomes

Selection feedback SHALL consume idempotently joined, verified, task-compatible checker outcomes within the 30-day window, weighted with a seven-day half-life. The score SHALL be `(weighted accepted + 0.5 * weighted checked-partial + 1) / (weighted eligible attempts + 2)`. The system SHALL retain an observed count of unique eligible checked attempts, use a 0.5 prior with zero observations, and demote only for that task kind when score is below 0.4 and observed count is at least three. Transport, infrastructure, authorization, quota, identity-mismatch, cancelled, and incomplete outcomes SHALL NOT enter the task-quality denominator.

#### Scenario: Sparse checked history
- **WHEN** a route has fewer than three unique eligible checked outcomes for a task kind
- **THEN** that history SHALL NOT demote the route for the task kind, and zero-observation quality SHALL remain unknown with the 0.5 prior

### Requirement: Metadata maintenance has one visible scheduled provenance

The system SHALL install and report one six-hour metadata refresh schedule through its supported setup and doctor paths only after an explicit setup opt-in or install-service action. Dry-run and declined setup SHALL perform no persistent writes, systemd changes, or provider calls. Refresh results SHALL preserve source identity, observation and fetch times, method and scale, explicit missingness, and valid original-deadline last-good facts when a source partially fails. Unsupported sources SHALL NOT be represented as live integrations.

#### Scenario: Partial scheduled refresh
- **WHEN** a scheduled refresh has a partial or malformed source result
- **THEN** valid fresh facts and still-valid last-good facts SHALL remain distinguishable by source provenance and age, and failed or missing facts SHALL NOT be fabricated

### Requirement: V1 acceptance remains explicit and falsifiable

The system SHALL evaluate all acceptance cases A01–A30 in the coordinated 13-story plan. Spec conformance, fixture tests, implementation completion, and live end-to-end proof SHALL remain distinct states; the system SHALL NOT claim delivery completion until the required merged-main proof and independent parent rerun are recorded.

#### Scenario: Incomplete acceptance evidence
- **WHEN** one or more required acceptance cases lack their required evidence
- **THEN** the corresponding delivery SHALL remain incomplete and SHALL NOT be represented as fully proven
