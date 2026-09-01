# Specification Quality Checklist: Cross-Repository Security and Privacy Launch Gate

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-31
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Items marked incomplete require spec updates before `/speckit-clarify` or `/speckit-plan`.

### Validation record

Three decisions that materially changed scope were resolved with the operator
before the requirements were written, rather than left as markers:

1. **Scope** — core and node parity, `verdict-core` landing first, mirrored
   into `verdict-node` as a separate pull request (FR-020 through FR-023).
2. **Dynamic verification** — scoped to the optional server surface, with CLI
   and deserialization fuzzing explicitly out of scope (FR-009, FR-010).
3. **Telemetry** — local-only with no egress, enforced by a failing test
   rather than asserted in prose (FR-012, SC-006).

### Deliberate wording choices

- Named artifacts (`THREAT_MODEL.md`, `PRIVACY_POLICY.md`) appear in FR-016
  and FR-017 because they are the deliverables that acceptance gates G5.1 and
  G5.2 cite by path. They are contract, not implementation choice.
- Requirements are phrased as capabilities ("refuse to publish when...")
  rather than tool invocations, so the plan phase remains free to choose
  tooling. The current tool inventory sits in the Overview table as context.
- "Blocking severity" is referenced abstractly throughout and pinned once in
  Assumptions, so the threshold can be revised without rewriting every
  requirement.

### Clarification session 2026-09-01

Four questions asked and integrated. Each was verified against the code before
being asked, so the options offered were real rather than hypothetical:

1. **Erasure vs. append-only** — erasure clears mutable stores; the evidence
   chain keeps non-reversible references and is never rewritten (FR-015,
   FR-015a, FR-015b, SC-010). Resolved a direct contradiction between the
   original FR-015 and the append-only design VER-011 shipped.
2. **Cross-repo policy sharing** — the blocking severity joins the existing
   compatibility contract (FR-023 through FR-025, SC-007). Verified that the
   `verdict compat` command exists and fails closed but is wired into no
   workflow, so this feature absorbs the half of ADR-024 still open.
3. **Integrity of the gate's own tooling** — every third-party step pinned to
   an immutable revision, enforced by a check (FR-005a through FR-005c,
   SC-011). Verified that nothing is SHA-pinned today and that the PyPI
   publishing step tracks a moving branch.
4. **Exception mechanism** — schema-validated tracked file; malformed and
   expired entries both behave as absent (FR-005, FR-005d through FR-005f).

### Resolved without a question

- Upstream dependencies from issue #238 were verified directly rather than
  asked about: VER-008 (#225), VER-011 (#228), and MEM-001 (#229) are closed.
  REL-001 has no tracked issue but its pipeline is in place. None constrains
  sequencing.
- Scan cadence was not asked; the existing security workflow already runs on
  pull request, push, weekly, and on demand, and the new checks inherit it.

### Deferred to `/speckit-plan`

- Bill-of-materials format and the specific generator.
- The concrete tool for dynamic verification of the server surface.
- Continuous-integration time budget and whether the heavier checks run on
  every pull request or only on the scheduled and release runs.
- The exception file's concrete schema and location in each repository.
