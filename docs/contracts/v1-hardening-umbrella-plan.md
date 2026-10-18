# V1 Hardening Coordinated Umbrella Delivery Plan (13-Story Planning Authority)

## Preservation Notice
- **Authority**: Linear issue `d568eb08...` (sections 22–27) and local `.codex/audits/2026-10-18/v1*.md`.
- **Status**: Governing planning contract for Verdict V1 across 7 waves (S01–S13, Acceptance A01–A30).
- **Preserved from OpenSpec commits**: `ef5cc112`, `dd1e859b` on branch `integration/v1-foundation`.
- **Reason**: Decouple multi-wave planning authority from per-wave OpenSpec active change lifecycle. Active changes in `openspec/changes/` represent landing changesets; the full 13-story umbrella roadmap remains preserved here byte-for-byte.

## Original File Checksums (SHA-256)
- `proposal.md`: `bc18c5fdda04ed897d9be23f3b3c1fae8d82942e4e61e804d0ec1f993bc2e96d`
- `design.md`: `b8b60119f4e715242c0fa55e1cab9faeb4c6096f85429309b4dcc3f6bd6edd56`
- `specs/v1-model-control/spec.md`: `cdfd51451690f640cf948f222637e832b2cd7290f94e83b86aeb77d736459df6`
- `tasks.md`: `f51694b40c972fcc4912c07ced38bdf22a55e0a393ccba2fcbf13697c703a975`

---

### Preserved Document: proposal.md
<!-- BEGIN PRESERVED: proposal.md (sha256:bc18c5fdda04ed897d9be23f3b3c1fae8d82942e4e61e804d0ec1f993bc2e96d) -->
# Proposal

## Why

The current V1 plan closes connected gaps between trusted inventory, model qualification, authorized dispatch, checked outcomes, and task-specific feedback. Existing component changes cover parts of this path, but no single contract captures the required end-to-end behavior and acceptance matrix.

## What Changes

- Define the V1 behavior across inventory, admission, selection, dispatch, streaming, outcome evidence, and feedback.
- Coordinate the existing 13-story delivery plan without duplicating component specifications or changing unrelated architecture.
- Preserve performance and task-fit selection without price preference, and consume only checked task outcomes using the recorded smoothed history policy.

## Capabilities

### New Capabilities
- `v1-model-control`: End-to-end V1 requirements for verified model control and feedback.

### Modified Capabilities

## Impact

Coordinates existing model inventory, admission, relay, orchestration, metadata, receipt, and outcome components. No dependency or public API is introduced by this specification alone. Story ownership, dependencies, and acceptance IDs are tracked in `tasks.md`.
<!-- END PRESERVED: proposal.md -->

---

### Preserved Document: design.md
<!-- BEGIN PRESERVED: design.md (sha256:b8b60119f4e715242c0fa55e1cab9faeb4c6096f85429309b4dcc3f6bd6edd56) -->
# Design

## Context

This is an umbrella contract for the reviewed V1 plan. Existing OpenSpec changes remain authoritative for their component details; this change records only cross-component behavior that must hold together. The plan and its A01–A30 matrix are the implementation source of truth.

## Goals / Non-Goals

**Goals:** Coordinate the 13 existing stories; retain their dependency order and explicit acceptance coverage; keep unknown evidence visible and authority boundaries intact.

**Non-Goals:** Replace existing component specs, introduce a second router/coordinator/ledger, claim any acceptance row already passes, or authorize deployment, service cutover, or DoD closure.

## Decisions

1. Keep one umbrella capability, `v1-model-control`, for observable cross-cutting guarantees. Existing active and canonical specs continue to own detailed component behavior; this delta adds only requirements not represented there as a coherent end-to-end contract.
2. Preserve one shared qualification coordinator and current evidence/receipt stores. All producers share scoped reservations and bounded budgets; unknown topology uses an exclusive Core lane. Unknown values remain explicit and never become positive evidence.
3. Apply admission and hard task/access constraints before ranking. Rank eligible choices by task performance and fit, with no price preference. Price is a hard constraint only when an explicit spend limit requires it.
4. Bind final sends and handoffs to scoped, unexpired permits. Preserve protocol terminal states and reported identity, close stream transports once, and join attempts to independent checked outcomes.
5. Feed selection only with verified task-matched outcomes in the checked 30-day window, weighted with a seven-day half-life. Demote only below 0.4 with at least three unique checked attempts; zero observations retain the 0.5 prior and unknown status.
6. Install one six-hour metadata refresh timer with visible source provenance through existing setup and doctor paths. Preserve the supported opt-in mirror and partial-source last-good facts under original deadlines.
7. Use the 13 story groups and their A01–A30 mappings in `tasks.md` as the delivery map. Implementation checkboxes remain open until the owning story performs its required work and validation.

## Risks / Trade-offs

- [Umbrella scope can drift into duplicate component design] → keep component requirements in their current changes and update this delta only for cross-component behavior.
- [Some upstream identity, retry, or pool-topology facts may be unavailable] → preserve explicit unknown/unsupported outcomes and bound claims to Core-coordinated behavior.
- [Integration proof depends on live operator state] → defer live acceptance and parent evidence to the dedicated final stories; fixtures and strict spec validation do not count as live proof.

## Migration Plan

Deliver stories in the waves in `tasks.md`, requiring verified merged-main prerequisites between dependent waves. Do not archive this change, mark implementation tasks complete, publish DoD evidence, or switch installed services until their own proof gates pass.
<!-- END PRESERVED: design.md -->

---

### Preserved Document: specs/v1-model-control/spec.md
<!-- BEGIN PRESERVED: specs/v1-model-control/spec.md (sha256:cdfd51451690f640cf948f222637e832b2cd7290f94e83b86aeb77d736459df6) -->
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

#### Scenario: Metadata maintenance was not selected
- **WHEN** the optional metadata timer is absent because the operator has not installed it
- **THEN** doctor SHALL report an explicit opt-in warning without treating the absent feature as healthy or failing unrelated diagnostics

#### Scenario: Timer triggered without a completed refresh
- **WHEN** an installed metadata timer has triggered but has no valid service-completion evidence
- **THEN** doctor SHALL report an incomplete refresh even if the service result still has its default success value

### Requirement: V1 acceptance remains explicit and falsifiable

The system SHALL evaluate all acceptance cases A01–A30 in the coordinated 13-story plan. Spec conformance, fixture tests, implementation completion, and live end-to-end proof SHALL remain distinct states; the system SHALL NOT claim delivery completion until the required merged-main proof and independent parent rerun are recorded.

#### Scenario: Incomplete acceptance evidence
- **WHEN** one or more required acceptance cases lack their required evidence
- **THEN** the corresponding delivery SHALL remain incomplete and SHALL NOT be represented as fully proven
<!-- END PRESERVED: specs/v1-model-control/spec.md -->

---

### Preserved Document: tasks.md
<!-- BEGIN PRESERVED: tasks.md (sha256:f51694b40c972fcc4912c07ced38bdf22a55e0a393ccba2fcbf13697c703a975) -->
# Tasks

## 1. Wave 1 — shared gateway binding and metadata schedule

- [ ] 1.1 **S01 / BOD-416** (related BOD-385), acceptance **A06**: unify credential and gateway binding across discovery and transport; verify canonical/stored credentials bind both callers identically, explicit URL precedence, safe default, and secret-free errors using the S01 focused tests.
- [ ] 1.2 **S08 / BOD-436** (related BOD-108, BOD-411), acceptance **A24**: install one six-hour metadata refresh timer with explicit source provenance; verify setup/doctor, partial-source retention, freshness, and recovery with isolated tests.

## 2. Wave 2 — task authority and bounded inventory

- [ ] 2.1 **S02 / BOD-432** (residual of BOD-417), depends on S01, acceptance **A07, A08, A25**: preserve server-owned task requirements through public entrypoints and make admitted ranking price-invariant; verify reserved-flag rejection, task-fit enforcement, and price-only invariance with the S02 focused tests.
- [ ] 2.2 **S04 / BOD-345** (related BOD-387, BOD-407), depends on S01, acceptance **A01, A03, A05**: bound inventory and parse scoped telemetry without healthy defaults; verify completeness, caps/cursors, aliases, unknowns, and observed zeros with the S04 focused tests.

## 3. Wave 3 — durable dispatch authority

- [ ] 3.1 **S03 / BOD-433** (related BOD-417, BOD-223), depends on S02 and S04, acceptance **A04, A10**: bind final dispatch permits to durable evidence epochs and retry handoffs; verify before/after-commit revocation, restart replay, scope isolation, expiry, and duplicate-attempt fencing with the S03 focused tests.

## 4. Wave 4 — shared qualification and protocol correctness

- [ ] 4.1 **S05 / BOD-297** (related BOD-292, BOD-310, BOD-350), depends on S03 and S04, acceptance **A09, A12, A13, A14, A15, A16**: route all qualification producers through one scoped coordinator and shared budgets; verify fairness, multi-process caps, crash recovery, fencing, joins, cancellation, and actual safe concurrency with S05 focused tests.
- [ ] 4.2 **S06 / BOD-434** (related BOD-417, BOD-353, BOD-84), depends on S02 and S03, acceptance **A17, A18, A19, A20, A21, A22, A23**: preserve SSE terminal semantics and route identity with close-once cleanup; verify fragmented streams, failure terminals, identity, bounded retries, cancellation, and independent check behavior with S06 focused tests.

## 5. Wave 5 — public projection and joined outcomes

- [ ] 5.1 **S07 / BOD-435** (related BOD-417, BOD-291, BOD-387), depends on S04, S05, and S06, acceptance **A02, A03, A11**: publish verified public models and recover readiness without request-time probes; verify exact freshness boundaries, unavailable startup/recovery, and zero catalog model calls with S07 focused tests.
- [ ] 5.2 **S09 / BOD-437** (related BOD-353, BOD-415), depends on S03 and S06, acceptance **A18, A26, A28**: join every attempt and independent checker result in the existing ledger; verify success/reject/retry/failure/cancel/incomplete joins, append-only checker links, and rejection of duplicate, future, tampered, or wrong-scope evidence with S09 focused tests.

## 6. Wave 6 — task history and installed runtime provenance

- [ ] 6.1 **S10 / BOD-438** (related BOD-304, BOD-353), depends on S02, S08, and S09, acceptance **A25, A27, A28**: consume checked task-specific history using the specified 30-day window, seven-day half-life, smoothing, and minimum-three demotion; verify restart/idempotency, transient exclusions, exact scores, and next-shortlist consumption with S10 focused tests.
- [ ] 6.2 **S11 / BOD-414** (related BOD-411, BOD-421, BOD-410), depends on S05, S07, and S08, acceptance **A29**: align installed acceptance and maintenance runtimes to the reviewed build; verify recorded build/config provenance and diagnose unexpected parallel loops with isolated installed-state checks.

## 7. Wave 7 — live and complete acceptance

- [ ] 7.1 **S12 / BOD-410** (related BOD-355), depends on S05, S06, S07, S10, and S11, acceptance **A23, A29, A30**: demonstrate bounded live maintenance across three cycles on merged main; record date, exact SHA, commands, raw results, freshness, and parent rerun evidence using the existing proof gate.
- [ ] 7.2 **S13 / BOD-439** (related BOD-355), depends on S01–S12, acceptance **A01–A30**: verify every matrix row against its owning evidence and close delivery only when required merged-main and independent parent proof is present; verify with the complete acceptance matrix and DoD checker.
<!-- END PRESERVED: tasks.md -->
