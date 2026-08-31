# Feature Specification: Cross-Repository Security and Privacy Launch Gate

**Feature Directory**: `specs/238-security-privacy-launch-gate`
**Issue**: [#238](https://github.com/mrnicholasbcarter-code/verdict-core/issues/238) (LAUNCH-001, P0, area:security)
**Created**: 2026-08-31
**Status**: Draft — ready for `/speckit-clarify`
**Repositories**: `verdict-core` (primary), `verdict-node` (parity)

## Overview

Verdict cannot claim launch readiness on assertion. Two of the twenty-nine
documented release gates — the security review (G5.1) and the privacy review
(G5.2) — are blocked today because the artifacts they cite do not exist, and
the acceptance-gate report correctly reports them as `BLOCKED` rather than
inventing a result.

This feature closes that gap by making security and privacy readiness a
**verifiable property of a release**, not a claim in a document. Release
publication must fail when a qualifying finding is unresolved, when the
software bill of materials cannot be produced, or when a privacy boundary
regresses. The same standard must hold in both repositories that ship
Verdict artifacts, so a user cannot obtain a weaker guarantee by installing
the TypeScript surface instead of the Python one.

### Why this is not already done

Substantial machinery exists and is not advisory: dependency advisories,
static analysis, a committed-credential scan, and build provenance on
publish. What is missing is the part that turns those signals into a gate:

| Capability | State today | Gap |
| --- | --- | --- |
| Dependency advisories | Enforced in `verdict-core` | Not enforced in `verdict-node` |
| Secret scanning | Enforced in `verdict-core` | Not enforced in `verdict-node` |
| Static analysis (SAST) | Enforced in both | Severity thresholds disagree between checks |
| Software bill of materials | **Absent everywhere** | No SBOM is produced or published |
| Build provenance | Produced on release in `verdict-core` | Not produced in `verdict-node` |
| Dynamic checks | **Absent** | The optional server surface is never exercised adversarially |
| Threat model | **Absent** | Gate G5.1 blocked |
| Privacy policy, retention, erasure | **Absent** | Gate G5.2 blocked |
| Telemetry egress guarantee | Believed to hold | Never asserted by a test |

The severity inconsistency is the sharpest of these: three different checks
currently apply three different thresholds, so "no high findings" is true of
some checks and unverified for others. A single stated policy, applied
uniformly, is what makes the gate meaningful.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — A release cannot ship an unresolved critical finding (Priority: P1)

A maintainer tags a release. A dependency in the tree has a newly published
critical advisory. The release pipeline refuses to publish, names the finding
and the package, and leaves every registry untouched. The maintainer resolves
the advisory and re-runs; publication proceeds.

**Why this priority**: This is the whole point of a launch gate. Without it,
every other artifact in this feature is documentation rather than enforcement.

**Acceptance scenarios**:

1. **Given** a dependency with an unresolved advisory at or above the blocking
   severity, **When** a release is attempted, **Then** publication does not
   occur, no artifact reaches any registry, and the failure names the package,
   the advisory identifier, and the severity.
2. **Given** the same advisory marked with a recorded, expiring exception,
   **When** a release is attempted, **Then** publication proceeds and the
   exception appears in the release evidence with its justification and expiry.
3. **Given** an expired exception, **When** a release is attempted, **Then**
   publication is refused exactly as if no exception existed.
4. **Given** a clean tree, **When** a release is attempted, **Then**
   publication proceeds and the security evidence is attached to the release.

### User Story 2 — A reviewer reproduces the security and privacy evidence (Priority: P1)

A reviewer clones the repository at a release tag and reproduces the security
and privacy evidence without credentials, without network access to any
private service, and without asking the maintainer what was run.

**Why this priority**: The issue's definition of done requires exactly this,
and gates G5.1 and G5.2 stay blocked until the artifacts they cite exist and
can be regenerated.

**Acceptance scenarios**:

1. **Given** a clean checkout at a release tag, **When** the reviewer runs the
   documented verification command, **Then** the threat model, the privacy
   policy, the bill of materials, and the scan results are all present and
   correspond to that exact commit.
2. **Given** the acceptance-gate report is regenerated, **When** it is
   verified, **Then** G5.1 and G5.2 report `PASS` citing real files rather
   than `BLOCKED`.
3. **Given** a published release, **When** a consumer inspects it, **Then** a
   bill of materials and a provenance attestation are available for every
   published artifact.

### User Story 3 — Private data cannot cross a boundary it was never meant to cross (Priority: P2)

An operator runs Verdict against work containing credentials and personal
data. Nothing sensitive is written to durable storage in recoverable form, and
no telemetry leaves the machine.

**Why this priority**: Redaction machinery already exists at these boundaries;
what is missing is the negative-path proof that it holds, plus the written
policy that says what the guarantee is. Enforcement without a stated
guarantee is not reviewable.

**Acceptance scenarios**:

1. **Given** content carrying credentials and personal data, **When** it
   crosses the memory boundary, **Then** the persisted record contains no
   recoverable secret, and a test fails loudly if it ever does.
2. **Given** any telemetry or observability path, **When** it is exercised,
   **Then** no network egress is attempted, and a test fails if one is.
3. **Given** a stored record, **When** the operator requests erasure, **Then**
   the record and its derivatives become unrecoverable, and the documented
   retention period is enforced without manual intervention.
4. **Given** malformed or oversized input at the optional server surface,
   **When** it is submitted, **Then** the surface rejects it without crashing,
   without leaking internal detail, and without persisting the payload.

### Edge Cases

- A scanner is unreachable or its feed is stale. The gate must fail closed —
  an unavailable scanner is not a pass — and must say which check could not
  be evaluated. This mirrors the existing rule that an unreachable surface
  produces `blocked`, never a pass.
- A finding is disputed or has no fix available. The exception mechanism must
  absorb this without requiring the blocking threshold to be lowered globally.
- A finding appears only in a development or documentation dependency that is
  never shipped. The gate must distinguish shipped from non-shipped
  dependencies rather than blocking a release over a test-only package.
- The two repositories disagree — a dependency is clean in one and flagged in
  the other. Each repository gates its own artifacts; neither may publish on
  the strength of the other's result.
- A release is retried after a partial failure. The gate must not treat
  already-published artifacts as permission to skip re-verification.
- The bill of materials cannot be generated for an artifact. That is a
  blocking condition, not a warning.

## Requirements *(mandatory)*

### Functional Requirements — Release gating

- **FR-001**: The release pipeline MUST refuse to publish any artifact when an
  unresolved finding at or above the blocking severity exists in dependency
  advisories, static analysis, or secret scanning.
- **FR-002**: The project MUST state one blocking severity policy, and every
  check MUST apply it. Where a check cannot express the policy natively, the
  pipeline MUST enforce it on the check's output.
- **FR-003**: A check that cannot be evaluated MUST block the release. Absence
  of a result MUST NOT be recorded as a passing result.
- **FR-004**: The gate MUST distinguish dependencies shipped to users from
  those used only to build or test, and MUST apply the blocking policy to
  shipped dependencies. Findings in non-shipped dependencies MUST be reported
  without blocking.
- **FR-005**: The project MUST provide a recorded exception mechanism carrying
  a justification, an owner, and an expiry. An expired exception MUST have no
  effect. Active exceptions MUST appear in the release evidence.

### Functional Requirements — Supply chain

- **FR-006**: A software bill of materials MUST be generated for every
  published artifact and published alongside it.
- **FR-007**: Failure to generate a bill of materials MUST block publication.
- **FR-008**: Every published artifact MUST carry a build provenance
  attestation that a consumer can verify against the source commit.

### Functional Requirements — Dynamic verification

- **FR-009**: The optional server surface MUST be exercised adversarially
  before release against unauthenticated access, injection, malformed input,
  and oversized payloads.
- **FR-010**: The server surface MUST reject invalid input without crashing,
  without disclosing internal detail in the response, and without persisting
  the rejected payload.

### Functional Requirements — Privacy and data governance

- **FR-011**: Content crossing the memory boundary MUST NOT persist
  credentials or personal data in recoverable form, and this MUST be asserted
  by tests that fail when it regresses.
- **FR-012**: Telemetry and observability data MUST remain on the local
  machine. No path may attempt network egress, and a test MUST fail if one
  does.
- **FR-013**: The project MUST document what data is stored, where, for how
  long, and how it is erased.
- **FR-014**: The documented retention period MUST be enforced by the software
  rather than by operator discipline.
- **FR-015**: An operator MUST be able to erase stored records and their
  derivatives, and erasure MUST be verifiable.

### Functional Requirements — Documentation and review

- **FR-016**: The project MUST publish a threat model naming assets, trust
  boundaries, adversaries, and the mitigation for each identified threat,
  satisfying acceptance gate G5.1.
- **FR-017**: The project MUST publish a privacy policy covering collection,
  storage, retention, erasure, and telemetry, satisfying acceptance gate G5.2.
- **FR-018**: Both documents MUST be verifiable from a clean checkout and MUST
  be cited as evidence by the acceptance-gate report.
- **FR-019**: A change to a trust boundary, a persistence format, or a
  provider or execution path MUST require review against the threat model
  before merge.

### Functional Requirements — Cross-repository parity

- **FR-020**: `verdict-node` MUST enforce dependency advisories, secret
  scanning, static analysis, and the same blocking severity policy as
  `verdict-core`.
- **FR-021**: `verdict-node` MUST produce a bill of materials and a provenance
  attestation for every published artifact.
- **FR-022**: Neither repository may publish on the strength of the other's
  verification result. Each gates its own artifacts.
- **FR-023**: The blocking severity policy MUST be expressed once and
  referenced by both repositories, so the two cannot silently diverge.

### Key Entities

- **Finding**: An issue raised by a check. Carries a severity, a source check,
  an affected component, whether that component ships to users, and an
  identifier a reviewer can look up.
- **Exception**: A recorded, expiring waiver for one finding. Carries a
  justification, an owner, and an expiry date.
- **Severity policy**: The single stated threshold at or above which a finding
  blocks a release, shared by both repositories.
- **Bill of materials**: The component inventory for one published artifact.
- **Provenance attestation**: The verifiable link from a published artifact
  back to the source commit that produced it.
- **Retention rule**: The category of stored data, its lifetime, and its
  erasure method.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A release carrying an unresolved finding at or above the
  blocking severity cannot be published — demonstrated by an attempt that is
  refused, with no artifact reaching any registry.
- **SC-002**: Acceptance gates G5.1 and G5.2 report `PASS` citing files that
  exist, raising the passing gate count without any gate being marked passed
  on absent evidence.
- **SC-003**: A reviewer starting from a clean checkout reproduces the full
  security and privacy evidence in under 15 minutes, with no credentials and
  no access to any private service.
- **SC-004**: Every published artifact in both repositories has a retrievable
  bill of materials and a provenance attestation that verifies against its
  source commit — 100% of artifacts, not a sample.
- **SC-005**: No credential or personal-data value is recoverable from durable
  storage after content carrying them crosses the memory boundary, across the
  full negative-path test set.
- **SC-006**: No telemetry or observability path attempts network egress,
  asserted by a test that fails if one is introduced.
- **SC-007**: Both repositories apply the identical blocking severity, and a
  change to that policy in one repository without the other is detected before
  merge.
- **SC-008**: Every documented retention period is enforced by the software,
  verified by a test that advances time and observes expiry.
- **SC-009**: The optional server surface survives the adversarial input set
  without crashing, disclosing internal detail, or persisting rejected input.

## Assumptions

- **Blocking severity is critical and high.** Medium and low findings are
  reported and tracked but do not block a release. This matches the strictest
  threshold already in use and avoids weakening any existing check.
- **Dynamic verification is scoped to the optional server surface.** It is the
  only network-reachable surface Verdict ships; the default install is a local
  CLI and library. Fuzzing of CLI parsing and memory-plane deserialization is
  out of scope for this feature and belongs to a separate workstream.
- **Telemetry is local-only with no egress.** This is the stance the project
  commits to and enforces by test. Inspection of the observability, gateway
  adapter, and suggestion paths found no direct outbound calls, so this
  formalizes and protects existing behavior rather than changing it.
- **`verdict-core` lands first, `verdict-node` follows.** The severity policy
  and the shared artifacts originate in `verdict-core`; the parity work is a
  separate pull request in `verdict-node` that references them. The two are
  never one commit, per the workspace repository-boundary rule.
- **Existing non-advisory checks are retained.** Nothing already enforced is
  relaxed or made advisory to accommodate the new policy.
- **Scan results are evidence, not prose.** Every claim in this gate is backed
  by a regenerable artifact, consistent with how the acceptance-gate report
  already treats evidence.
- **The acceptance-gate workflow remains a launch-readiness report, not a
  pull-request check.** This feature changes what it can report, not when it
  runs.

## Out of Scope

- Penetration testing or third-party security audit engagement.
- Fuzzing of CLI argument parsing or memory-plane deserialization.
- Runtime intrusion detection, or any always-on monitoring service.
- Compliance certification (SOC 2, ISO 27001) — the artifacts here may support
  such an effort but this feature does not pursue one.
- Provider-side security posture. Verdict ships no provider credentials and
  makes no claim about any provider's controls.
- Changes to routing, eligibility, or envelope enforcement semantics. This
  feature adds verification around them and must not alter them.

## Dependencies

- **Acceptance gates G5.1 and G5.2** (`ACCEPTANCE_GATES.md`) — the consumers
  of this feature's documentation artifacts.
- **The acceptance-gate report generator** — must cite the new artifacts as
  evidence once they exist.
- **The release pipeline** — already produces provenance; this feature adds
  the bill of materials and the blocking policy to it.
- **`verdict-node`** — the parity repository, gated separately.
- Issue #238 records upstream dependencies on VER-008, VER-011, MEM-001, and
  REL-001. Their status must be confirmed during planning; any that is
  incomplete constrains sequencing rather than scope.

## Constitutional Alignment

- **Fail-closed**: FR-003 makes an unevaluable check a blocking condition,
  matching the existing rule that an unreachable surface yields `blocked`,
  never a pass.
- **Evidence over assertion**: Every success criterion names a regenerable
  artifact or an executable test rather than a document to be trusted.
- **Advisory inputs cannot weaken hard policy**: FR-005 confines waivers to
  recorded, expiring, individually justified exceptions. There is no mechanism
  by which a scanner's opinion, a provider signal, or a learning input can
  lower the blocking threshold.
- **Determinism and offline reproducibility**: SC-003 requires the evidence to
  be reproducible without credentials or private services.
