# Feature Specification: Cross-Repository Security and Privacy Launch Gate

**Feature Branch**: `feat/238-launch-001`

**Created**: 2026-09-04

**Status**: Draft

**Input**: User description: "[LAUNCH-001] Cross-repository security and privacy launch gate (issue #238): CI runs dependency, secret, SAST, dynamic, SBOM, and provenance checks; memory boundaries have PII/secret tests; retention/erasure/telemetry consent documented and tested; critical/high findings block release."

## Clarifications

### Session 2026-09-04

- Q: What data retention/erasure SLA should the launch gate's automated test verify, and which regulatory framework should it be scoped against? → A: 30 days, GDPR-equivalent ("without undue delay, and in any case within 30 days")
- Q: What should the dynamic security check actually run against — a real deployed instance, or a local/ephemeral run of the built artifact? → A: Local/sandboxed container run — no staging environment provisioned; the built artifact runs in an isolated local container/process within CI and its exposed surface is scanned there.
- Q: If the launch-gate CI infrastructure itself is down or unreachable (not a single check failing, but the whole pipeline unavailable), who if anyone is authorized to approve a release without gate evidence? → A: A named, pre-designated emergency approver role may approve a release during a confirmed infrastructure outage, but must record an attributed waiver — the same mechanism as a per-finding waiver, applied to "gate unavailable" as a whole.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Release is blocked without complete supply-chain evidence (Priority: P1)

A release engineer prepares to publish a new version. Before the release can proceed, the pipeline must produce a Software Bill of Materials (SBOM) and a build provenance attestation for the release artifact, and must run a dynamic (runtime) security check against the built artifact. If any of these steps fails, is skipped, or reports a critical/high finding, the release is blocked.

**Why this priority**: This is the core gate — without it, a release can ship without knowing what it contains or whether it was tampered with in the build pipeline. It is the largest remaining gap identified in the existing launch-gate review (issue #271 already covers dependency/secret/SAST scanning as blocking gates; SBOM, provenance, and dynamic checks are still missing).

**Independent Test**: Trigger a release build. Confirm the pipeline produces an SBOM and provenance record, runs a dynamic check, and that a deliberately-introduced critical finding in any of the three stops the release before publish.

**Acceptance Scenarios**:

1. **Given** a release build with no known vulnerabilities, **When** the launch gate runs, **Then** an SBOM and a provenance attestation are produced and attached to the release, and the dynamic check passes.
2. **Given** a release build where SBOM generation fails (tool error, missing dependency graph), **When** the launch gate runs, **Then** the release is blocked and the failure is reported as a launch-gate evidence entry, not silently skipped.
3. **Given** a release build where the dynamic check reports a critical finding, **When** the launch gate runs, **Then** the release is blocked until the finding is resolved or explicitly waived by a reviewer with a recorded reason.

---

### User Story 2 - Memory and learning subsystems cannot leak PII or secrets (Priority: P1)

A reviewer needs assurance that any subsystem which stores, retrieves, or learns from user-provided content (memory, retrieval, advisory learning) cannot retain or surface personally identifiable information or secrets (API keys, credentials, tokens) across boundaries it shouldn't cross.

**Why this priority**: Memory/learning subsystems are a named, specific risk in the feature request — an ungated leak here is a direct privacy and security incident, and the constitution already requires that no learning/retrieval/provider data can weaken hard policy or bypass enforcement.

**Independent Test**: Feed representative PII- and secret-shaped content into each memory/learning boundary; assert none of it is retrievable outside its authorized scope, and that the test suite blocking release fails if any of it leaks.

**Acceptance Scenarios**:

1. **Given** content containing a synthetic secret (e.g., a fake API-key-shaped string) is written to a memory/learning boundary, **When** the boundary is queried from an unauthorized scope, **Then** the secret is not returned and the test asserting this is part of the blocking release-gate suite.
2. **Given** content containing synthetic PII is processed by an advisory/learning path, **When** the release-gate PII/secret test suite runs, **Then** it fails closed (reports a failure, not a pass) if any PII is retrievable beyond its declared retention/access boundary.

---

### User Story 3 - Data retention, erasure, and telemetry consent are documented and verifiably enforced (Priority: P2)

A compliance reviewer or end user needs to know, and verify, how long data is retained, how it can be erased on request, and whether telemetry collection honors consent (opt-in/opt-out).

**Why this priority**: Required by the launch gate's acceptance criteria, but scoped below P1 because it is a documentation-plus-verification requirement rather than a pipeline gate that blocks every release — it is tested once per relevant code path rather than re-evaluated on every build.

**Independent Test**: Submit a synthetic erasure request and confirm the documented retention/erasure policy's SLA is met by an automated test; toggle telemetry consent off and confirm no telemetry is emitted.

**Acceptance Scenarios**:

1. **Given** a documented data retention and erasure policy, **When** an erasure request is simulated in a test, **Then** the data is verifiably removed within the documented window and the test is part of the release-gate suite.
2. **Given** a user has not opted into telemetry, **When** the system runs, **Then** no telemetry is transmitted, and an automated test verifies this for both the opt-out and opt-in states.

---

### Edge Cases

- What happens when a launch-gate check tool is unavailable (network outage, missing binary) rather than failing with a finding? The gate MUST treat "unavailable" the same as "failed" — it MUST NOT default to a pass.
- What happens when a critical/high finding is a known false positive? A reviewer MUST be able to record an explicit, attributed waiver; the release MUST NOT proceed on an unrecorded or automatic suppression.
- What happens when the dynamic check target (the built artifact) itself fails to start? The gate MUST report this as a blocking failure, not skip the dynamic-check stage.
- What happens on an emergency/hotfix release under time pressure? The same evidence requirements apply; there is no silent bypass path — only an explicit, attributed, recorded waiver per the finding-level exception above.
- What happens when the launch-gate CI infrastructure itself is down or unreachable, blocking every release rather than one check failing? A named, pre-designated emergency approver role MAY approve a release under this condition, but only by recording an attributed waiver for "gate unavailable" — the same accountability mechanism as a per-finding waiver, never a silent or automatic bypass.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The release pipeline MUST generate a Software Bill of Materials (SBOM) for every release artifact.
- **FR-002**: The release pipeline MUST generate a build provenance attestation for every release artifact, recording source revision and build environment.
- **FR-003**: The release pipeline MUST run a dynamic (runtime) security check against the built release artifact running in an isolated local/sandboxed container or process within CI (no staging deployment required) before publish.
- **FR-004**: SBOM generation, provenance generation, and the dynamic check MUST be non-advisory (blocking) launch-gate stages, consistent with the existing blocking dependency/secret/SAST gates.
- **FR-005**: Any subsystem that stores, retrieves, or learns from user-provided content MUST have automated tests proving PII and secrets do not cross unauthorized boundaries, and these tests MUST be part of the blocking release-gate suite.
- **FR-006**: The system MUST document a data retention period and erasure procedure scoped to a GDPR-equivalent standard ("without undue delay, and in any case within 30 days" of a request), and MUST have an automated test verifying erasure requests are honored within that 30-day window.
- **FR-007**: The system MUST document its telemetry consent behavior (what is collected, opt-in/opt-out mechanics) and MUST have an automated test verifying no telemetry is transmitted without consent.
- **FR-008**: Reviewers MUST be able to reproduce every launch-gate evidence artifact (SBOM, provenance, dynamic-check result, PII/secret test result, retention/erasure test result) from a clean checkout, consistent with the existing launch-checklist evidence standard.
- **FR-009**: Any critical or high-severity finding from any launch-gate check MUST block release; an unavailable, partial, or degraded check result MUST NOT be interpreted as a pass.
- **FR-010**: A blocked release MUST only be able to proceed via an explicit, attributed, recorded reviewer waiver at the individual-finding level — never a blanket or silent bypass.
- **FR-011**: When the launch-gate infrastructure itself is unavailable (rather than a single check failing), only a named, pre-designated emergency approver role MAY authorize a release to proceed, and only by recording an attributed waiver for the outage — the same accountability standard as a per-finding waiver.

### Key Entities

- **Launch Gate Evidence Record**: Per-release aggregate of SBOM, provenance attestation, dynamic-check result, PII/secret boundary test result, retention/erasure test result, and any recorded waivers with reviewer attribution.
- **SBOM Artifact**: The generated bill of materials for a release build.
- **Provenance Attestation**: The build-origin record (source revision, build environment) for a release artifact.
- **Memory Boundary Test Result**: Pass/fail record from PII/secret leakage testing of a specific memory or learning subsystem boundary.
- **Retention & Erasure Policy Record**: Documented retention window and erasure SLA, plus the test evidence that it is honored.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of release builds carry an attached SBOM and provenance attestation before publish.
- **SC-002**: Zero releases publish with an unresolved critical or high-severity finding from any launch-gate check (dependency, secret, SAST, dynamic, PII/secret boundary).
- **SC-003**: A reviewer with no prior context can reproduce the complete launch-gate evidence set for any release from a clean checkout in under 30 minutes.
- **SC-004**: 100% of code paths in memory/learning subsystems that handle user-provided content are covered by an automated PII/secret boundary test in the blocking release-gate suite.
- **SC-005**: 100% of tested erasure-request and telemetry-consent scenarios (opt-in, opt-out, erasure-on-request) verifiably behave as documented.

## Assumptions

- The existing dependency, secret, and SAST scanning gates delivered under issue #271 (pip-audit, bandit, npm audit, osv-scanner, CodeQL as non-advisory gates) remain in place; this feature extends them with SBOM, provenance, dynamic checks, memory-boundary PII/secret tests, and retention/erasure/consent verification rather than replacing them.
- SBOM output uses an industry-standard format (e.g., CycloneDX or SPDX); the exact format is a planning-phase decision, not a scope-defining one.
- Provenance follows an established supply-chain attestation approach (e.g., SLSA/in-toto style); the exact mechanism is a planning-phase decision.
- "Dynamic check" means automated runtime/DAST-style scanning of the built artifact's exposed surface while it runs in an isolated local/sandboxed container or process within CI, not a deployed staging instance and not manual penetration testing.
- Data retention/erasure SLA is 30 days, scoped to a GDPR-equivalent standard, per the Clarifications session above; this is a decision, not an open default.
- Cross-repository evidence aggregation (verdict-node, verdict-ecosystem) depends on the REL-001 compatibility-manifest work landing separately; this feature's scope is verdict-core's own release pipeline and its documented dependencies (VER-008, VER-011, MEM-001, REL-001).
- "Telemetry" refers to any data collection already present in the product; this feature adds documentation and verification, not new data collection.
