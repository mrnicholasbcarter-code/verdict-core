# Proposal: Adopt OpenSpec for Verdict significant-change contract

## Why

Verdict V2 needs a structured, executable significant-change contract that preserves
the proven BOD-167 Spec Kit Lite required fields while leveraging OpenSpec's tooling
for proposal/spec/design/task lifecycle instead of maintaining a separate home-grown
workflow. This operationalizes the ideas proven in BOD-167.

**KNOWN**: BOD-167 (Linear issue d238bb26) established the canonical Spec Kit Lite
contract with 14 required fields. BOD-204 (Linear issue 59a32b39) requires adopting
OpenSpec with a versioned custom schema `verdict-change-v1` that carries the BOD-167
contract forward.

**KNOWN**: OpenSpec 1.13.2 is the latest published version (verified with npm view
@fission-ai/openspec version) and supports custom schema creation via `schema fork`.

**INFERRED**: The mapping from BOD-167 fields to OpenSpec artifacts can preserve all
required fields while following OpenSpec's natural proposal→specs→design→tasks flow.

## What Changes

- Initialize OpenSpec in verdict-ecosystem repository with `--tools none` (repo-local only)
- Create verdict-change-v1 custom schema (fork of spec-driven) with BOD-167 field mapping
- Add deterministic validator scripts/validate_openspec_change.py (stdlib only, unittest style)
- Add sample valid change (bod-204-openspec-foundation itself) and malformed test fixture
- Add tests/test_openspec_change.py with field-by-field validation tests
- Add docs/OPENSPEC.md documenting authority boundaries, triggers, exemptions, and exact commands
- Link docs/OPENSPEC.md from README and SPEC_KIT_LITE.md (superseded note)
- No mass migration of historical specs/ADRs

## Capabilities

### New Capabilities
- `openspec-integration`: OpenSpec adoption for verdict-change-v1 schema with deterministic validation

### Modified Capabilities
<!-- No existing capability requirements are changing -->

## Acceptance criteria

- [ ] OpenSpec initialized in repo (openspec/ directory) with verdict-change-v1 schema
- [ ] All BOD-167 Spec Kit Lite required fields mapped to verdict-change-v1 artifacts
- [ ] Deterministic validator scripts/validate_openspec_change.py passes on valid sample
- [ ] Deterministic validator fails on malformed fixture with precise reason
- [ ] tests/test_openspec_change.py passes with unittest
- [ ] docs/OPENSPEC.md documents authority boundaries clearly
- [ ] docs/OPENSPEC.md linked from README and SPEC_KIT_LITE.md
- [ ] All repo CI checks pass (check_local_links, unittest, check_adr_lifecycle, etc.)
- [ ] Fresh clone proof: clone branch, validate sample (pass), validate malformed (fail)
- [ ] PR opened with exact commands/outputs and head SHA

## Impact

Affected:
- verdict-ecosystem repository structure (new openspec/ directory)
- Documentation (new OPENSPEC.md, links from README and SPEC_KIT_LITE.md)
- Developer workflow (significant changes use OpenSpec instead of manual Spec Kit Lite)
- CI/validation (new test suite for OpenSpec changes)

Not affected:
- Historical specs/ and ADR files (remain as evidence, not migrated)
- Spec Kit Lite definition (marked superseded but not deleted)
- Linear, Git/GitHub, or Verdict runtime authority
