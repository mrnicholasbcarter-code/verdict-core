# Design

## Context

verdict-ecosystem already has:
- BOD-167 Spec Kit Lite contract at docs/SPEC_KIT_LITE.md with 14 required fields
- Existing test suite using unittest framework
- Scripts using Python stdlib only (no external dependencies)
- CI checks: check_local_links, unittest discover, check_adr_lifecycle, verify_adr_sources,
  check_ownership_phrases, secret_scan, inventory_check

OpenSpec 1.13.2 provides:
- Custom schema support via `schema fork` command
- Project-local schema storage at openspec/schemas/<name>/
- Artifact templates (proposal/spec/design/tasks) in Markdown
- Validation support (though we need deterministic field checking)

## Goals / Non-Goals

**Goals:**
- Adopt OpenSpec with minimal ceremony (--tools none for repo-local only)
- Map all BOD-167 fields to verdict-change-v1 artifacts
- Provide deterministic validation matching existing script style
- Preserve authority boundaries (Linear/OpenSpec/Git/Verdict separation)

**Non-Goals:**
- Fork OpenSpec itself
- Mass-migrate historical specs or ADRs
- Add tool integrations (.claude/, .codex/, etc.) beyond repo-local
- Replace Linear, Git/GitHub, or Verdict authorities

## Decisions

**Decision 1: Fork spec-driven schema to verdict-change-v1**

Rationale: OpenSpec's `schema fork` creates a project-local customizable schema.
This gives us control over templates and field requirements while using OpenSpec's
lifecycle tooling.

Alternative considered: Use spec-driven as-is and layer validation on top.
Rejected because we need to customize templates to include evidence labels and
map BOD-167 fields explicitly.

**Decision 2: Initialize with --tools none**

Rationale: BOD-204 requirements state "repo-local only" and to review any generated
tool configs. Using --tools none prevents OpenSpec from writing .claude/, .codex/,
etc. directories.

Alternative considered: Use --tools all and selectively delete. Rejected because
explicit exclusion is clearer and prevents accidental global config modifications.

**Decision 3: Deterministic validator as separate script (not just `openspec validate`)**

Rationale: We need to enforce specific BOD-167 field presence (evidence labels,
acceptance criteria format, etc.) that OpenSpec's built-in validator doesn't check.
The stdlib-only requirement also matches our existing script style.

Alternative considered: Rely solely on openspec validate. Rejected because it
doesn't enforce our custom required fields and would require external dependencies.

**Decision 4: Map BOD-167 fields across OpenSpec artifacts**

Mapping:
- proposal.md: Why, What Changes, Capabilities, Impact + evidence boundary labels
- spec.md: Requirements (SHALL/MUST), scenarios (WHEN/THEN), compatibility, failures
- design.md: Context, Goals/Non-goals, Decisions, Risks, Migration, ADR impact
- tasks.md: Numbered groups, verification per task, ownership, dependencies

Rationale: This preserves all 14 BOD-167 required fields while following OpenSpec's
natural artifact flow. Each OpenSpec artifact has a clear responsibility that maps
to a subset of BOD-167 fields.

## Risks / Trade-offs

**Risk: OpenSpec version updates could break custom schema**
→ Mitigation: Pin to 1.13.2 with `npx -y @fission-ai/openspec@1.13.2`. Document the
pinned version in OPENSPEC.md. Future updates require explicit validation.

**Risk: Developers might use OpenSpec built-in validation only**
→ Mitigation: Document that scripts/validate_openspec_change.py is required for
verdict-change-v1 compliance. CI runs it alongside openspec validate.

**Risk: Field mapping might miss a BOD-167 requirement**
→ Mitigation: Create field-by-field mapping table in docs/OPENSPEC.md and test
each field individually in tests/test_openspec_change.py.

**Trade-off: Two validation layers (OpenSpec + our validator)**

We run both openspec validate (for schema structure) and our custom validator
(for required fields). This is extra overhead but ensures both OpenSpec compliance
and BOD-167 field coverage.

## Migration Plan

1. Initialize OpenSpec with `npx -y @fission-ai/openspec@1.13.2 init --tools none`
2. Fork schema: `npx -y @fission-ai/openspec@1.13.2 schema fork spec-driven verdict-change-v1`
3. Update openspec/config.yaml to use verdict-change-v1 and add Verdict context
4. Create scripts/validate_openspec_change.py (stdlib validator)
5. Create sample change openspec/changes/bod-204-openspec-foundation/ (this change itself)
6. Create malformed fixture tests/fixtures/openspec/malformed-change/
7. Create tests/test_openspec_change.py with unittest assertions
8. Create docs/OPENSPEC.md with authority boundaries, triggers, and field mapping
9. Link from README.md and docs/SPEC_KIT_LITE.md
10. Run all CI checks
11. Fresh-clone proof in temp directory
12. Commit, push, open PR

Rollback: Delete openspec/ directory, scripts/validate_openspec_change.py,
tests/test_openspec_change.py, docs/OPENSPEC.md, and revert README/SPEC_KIT_LITE links.
SPEC_KIT_LITE.md remains authoritative until PR merges.

## ADR impact

No existing ADR covers change contract tooling. This change does not require a new ADR
because it's an operational workflow adoption, not an architectural decision that
affects runtime behavior or cross-repository contracts.

If future work requires ADR-level decisions (e.g., "all repositories MUST use
verdict-change-v1"), that would require a new ADR at that time.

## Security / trust implications

OpenSpec runs via npx, executing published npm package code. Pinning to @fission-ai/openspec@1.13.2
provides reproducibility but trusts npm registry and package integrity.

Mitigations:
- Pin exact version (1.13.2)
- No global installs (npx -y runs in isolated cache)
- No tool integrations (--tools none prevents writing to home directory configs)
- Review any generated files before committing

The validator scripts/validate_openspec_change.py runs user-provided change directories
as input. It only reads files (no writes, no execution). No shell expansion is used.

## Routing / context / memory implications

Not applicable - this change affects development workflow and documentation only,
not runtime routing, context, or memory behavior.
