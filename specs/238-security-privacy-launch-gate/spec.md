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
| Cross-repo policy gate | Command exists, fail-closed | Not wired into CI in either repo (ADR-024 half-done) |
| Integrity of the gate's own tooling | No third-party step is pinned to an immutable revision | The publishing step tracks a moving branch |
| Threat model | **Absent** | Gate G5.1 blocked |
| Privacy policy, retention, erasure | **Absent** | Gate G5.2 blocked |
| Telemetry egress guarantee | Believed to hold | Never asserted by a test |

The severity inconsistency is the sharpest of these: three different checks
currently apply three different thresholds, so "no high findings" is true of
some checks and unverified for others. A single stated policy, applied
uniformly, is what makes the gate meaningful.

## Clarifications

### Session 2026-09-01

- Q: When an operator asks to erase stored data, what happens to the
  append-only evidence chain that references it? → A: Erasure removes payloads
  from mutable stores; the evidence chain keeps only non-reversible references
  (hashes, ids, decisions) and stays append-only. Those references are not
  treated as personal data.
- Q: How is the blocking severity policy shared between the two repositories
  so they cannot silently diverge? → A: It becomes part of the existing
  cross-repo compatibility contract, so `verdict compat check` fails on
  divergence before merge. This requires completing the CI wiring and
  downstream declaration that ADR-024 currently leaves open.
- Q: What integrity requirement applies to the third-party tooling the gate
  itself pulls in? → A: Every third-party step is pinned to an immutable
  revision, and a check fails the build when any is unpinned or floating on a
  mutable tag. Verified 2026-09-01: nothing is pinned that way today, and the
  PyPI publishing step tracks a moving branch.
- Q: Where are security exceptions recorded, and what makes one valid? → A: A
  tracked file per repository with a validated schema — finding id,
  justification, owner, expiry. A malformed or expired entry fails the gate
  like an unwaived finding, and every active exception is copied into the
  release evidence.

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
   its content becomes unrecoverable from every mutable store, the evidence
   chain retains only a non-reversible reference and still verifies, and the
   documented retention period is enforced without manual intervention.
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
- An exception file entry is malformed, or its expiry has passed. Both cases
  behave as if no exception existed, so a waiver cannot be obtained by
  corrupting the record or by letting it go stale.
- A pinned third-party step gains its own security advisory. The pin must be
  updatable without weakening the pinning requirement, so the update path is
  a new immutable revision rather than a temporary float.
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
- An erasure request names a record the evidence chain references. The payload
  is erased from the mutable store; the chain keeps its non-reversible
  reference and is not rewritten, so previously issued receipts still verify.
- An erasure request names a record that does not exist, or one already
  erased. The request succeeds without error and reports that nothing
  remained, so erasure is safe to retry.

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
- **FR-005**: The project MUST record exceptions in a tracked file in each
  repository, validated against a schema. Each entry MUST carry the finding
  identifier, a justification, an owner, and an expiry date.
- **FR-005d**: A malformed exception entry MUST fail the gate exactly as an
  unwaived finding does. An exception cannot be granted by writing an
  unparseable record.
- **FR-005e**: An expired exception MUST have no effect, and expiry MUST be
  evaluated against the build's own clock rather than trusted from the record.
- **FR-005f**: Every active exception MUST be copied into the release evidence
  with its justification, owner, and expiry, so a reviewer sees what was
  waived without reading the repository's history.

### Functional Requirements — Integrity of the gate itself

- **FR-005a**: Every third-party tool or automation step the gate invokes MUST
  be pinned to an immutable revision. A mutable reference — a branch or a
  movable tag — MUST NOT be used in any workflow that builds, verifies, or
  publishes an artifact.
- **FR-005b**: A check MUST fail the build when any third-party step is
  unpinned, and it MUST name the offending step. The gate is held to the
  standard it enforces.
- **FR-005c**: The publishing step MUST be pinned like any other. It currently
  tracks a moving branch, which is the highest-risk instance of this class and
  MUST be remediated by this feature.

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
- **FR-015**: An operator MUST be able to erase stored records, and erasure
  MUST be verifiable. Erasure removes payloads from mutable stores. The
  evidence chain remains append-only and is never rewritten.
- **FR-015a**: The evidence chain MUST retain only non-reversible references —
  hashes, identifiers, and decision outcomes — for erased records. It MUST NOT
  hold any value from which erased content can be reconstructed.
- **FR-015b**: Erasure MUST NOT invalidate a previously issued receipt, and
  chain verification MUST still succeed after any erasure.

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
- **FR-023**: The blocking severity policy MUST be carried by the existing
  cross-repo compatibility contract, so a divergence between the two
  repositories fails the compatibility gate before merge rather than being
  discovered later.
- **FR-024**: The compatibility gate MUST run in the continuous integration of
  both repositories. It exists today as a fail-closed command but is not wired
  into any workflow, so this feature MUST complete that wiring and the
  downstream declaration ADR-024 leaves open.
- **FR-025**: A change to the blocking severity in one repository without the
  corresponding change in the other MUST fail that gate, and the failure MUST
  name both the expected and the declared threshold.

### Key Entities

- **Finding**: An issue raised by a check. Carries a severity, a source check,
  an affected component, whether that component ships to users, and an
  identifier a reviewer can look up.
- **Exception**: A recorded, expiring waiver for one finding, held in a
  schema-validated tracked file. Carries the finding identifier, a
  justification, an owner, and an expiry date. Malformed and expired entries
  are both treated as absent.
- **Severity policy**: The single stated threshold at or above which a finding
  blocks a release, shared by both repositories.
- **Bill of materials**: The component inventory for one published artifact.
- **Provenance attestation**: The verifiable link from a published artifact
  back to the source commit that produced it.
- **Retention rule**: The category of stored data, its lifetime, and its
  erasure method. Each rule declares whether its store is mutable (erasable)
  or append-only (retains non-reversible references only).
- **Non-reversible reference**: A hash, identifier, or decision outcome the
  evidence chain retains for an erased record. It is not personal data and
  cannot be used to reconstruct the erased content.

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
  change to that policy in one repository without the other fails the
  compatibility gate before merge — demonstrated by a deliberate one-sided
  change that is refused.
- **SC-008**: Every documented retention period is enforced by the software,
  verified by a test that advances time and observes expiry.
- **SC-010**: After erasure, the erased content is unrecoverable from every
  mutable store, while evidence-chain verification still succeeds and every
  previously issued receipt still verifies — demonstrated on the same record.
- **SC-009**: The optional server surface survives the adversarial input set
  without crashing, disclosing internal detail, or persisting rejected input.
- **SC-011**: Every third-party step in every workflow that builds, verifies,
  or publishes is pinned to an immutable revision — 100%, verified by a check
  that fails on the first exception.

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
- **The evidence chain stays append-only.** Erasure is a property of the
  mutable stores, not of the chain. This preserves the guarantee VER-011
  (#228) shipped — a receipt is worth something precisely because history
  cannot be quietly rewritten — and it holds only because FR-011 already
  forbids sensitive values from entering the chain in recoverable form.
- **Scan results are evidence, not prose.** Every claim in this gate is backed
  by a regenerable artifact, consistent with how the acceptance-gate report
  already treats evidence.
- **Scan cadence follows the existing security workflow.** It already runs on
  pull request, on push, weekly, and on demand; the new checks join it rather
  than introducing a separate schedule. Artifact-bound outputs — the bill of
  materials and provenance — are produced at release, since there is no
  artifact to describe before then.
- **Exceptions are evaluated offline.** The exception record is a file in the
  repository, so the gate never depends on a network service to decide whether
  a finding is waived, and it continues to fail closed without connectivity.
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
- **ADR-024 (cross-repo compatibility gate)** — status "Partially
  Implemented". The manifest and the fail-closed command exist in
  `verdict-core`; the downstream declaration and the CI wiring do not. FR-023
  through FR-025 depend on that half being completed here, which is scope this
  feature absorbs rather than inherits.
- **The acceptance-gate report generator** — must cite the new artifacts as
  evidence once they exist.
- **The release pipeline** — already produces provenance; this feature adds
  the bill of materials and the blocking policy to it.
- **`verdict-node`** — the parity repository, gated separately.
- Issue #238 records upstream dependencies on VER-008, VER-011, MEM-001, and
  REL-001. Verified 2026-09-01: VER-008 (#225), VER-011 (#228), and MEM-001
  (#229) are all closed. REL-001 has no tracked issue, but the release
  pipeline it names is in place — provenance attestation, version
  verification, and documented recovery. None of these constrains sequencing.

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
