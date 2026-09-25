# Spec Delta

## Purpose

Provides executable significant-change contract for Verdict using OpenSpec with
verdict-change-v1 custom schema that preserves all BOD-167 Spec Kit Lite required fields.

## ADDED Requirements

### Requirement: verdict-change-v1 schema SHALL map all BOD-167 required fields

The verdict-change-v1 custom schema SHALL preserve every required field from BOD-167
Spec Kit Lite contract across its proposal/specs/design/tasks artifacts.

Required mapping:
- proposal.md: Linear issue reference, goal/outcome, evidence boundary (KNOWN/INFERRED/NOT AVAILABLE),
  scope, non-goals, acceptance criteria
- spec.md: observable requirements, scenarios, compatibility, failure behavior  
- design.md: contracts, architecture, security/trust, routing/context/memory implications,
  concurrency, migration/rollback, ADR impact
- tasks.md: bounded units with repo/write ownership, dependencies, required proof

#### Scenario: BOD-167 field mapping is complete
- **WHEN** validator checks a verdict-change-v1 change
- **THEN** all 14 BOD-167 required fields are either present in artifacts or explicitly mapped

### Requirement: Deterministic validator SHALL enforce verdict-change-v1 contract

The validator at scripts/validate_openspec_change.py SHALL check required artifacts
and fields for verdict-change-v1 and exit non-zero with precise reason on failure.

#### Scenario: Valid change passes validation
- **WHEN** validator runs on a complete verdict-change-v1 change
- **THEN** validator exits 0 and prints success message

#### Scenario: Missing artifact fails validation
- **WHEN** validator runs on change missing required artifact (proposal/specs/design/tasks)
- **THEN** validator exits 1 and prints which artifact is missing

#### Scenario: Missing required heading fails validation
- **WHEN** validator runs on change with incomplete artifacts
- **THEN** validator exits 1 and prints which heading is missing

### Requirement: Authority boundaries SHALL be preserved

OpenSpec adoption SHALL NOT make Prime Agent or OpenSpec an authority over planning,
source truth, or execution verification.

Authority boundaries:
- Linear: planning, priority, work state
- OpenSpec: change contract structure and artifacts
- Git/GitHub: source, PR, merge truth
- Verdict: plan/admit/execute/verify/review/receipt
- Prime Agent: execution harness (NOT an OpenSpec authority)

#### Scenario: Documentation states authority boundaries clearly
- **WHEN** reading docs/OPENSPEC.md
- **THEN** each authority boundary is explicitly stated

### Requirement: Significant-change triggers SHALL match BOD-167

The verdict-change-v1 schema SHALL require the same significant-change triggers as
BOD-167: public behavior, architecture, cross-repo/package/schema contract,
security/trust, routing/context/memory semantics, persistence/migration,
deployment/release, benchmark/public claim, or multi-repo change.

Small local refactors and typo-only docs changes SHALL be exempt.

#### Scenario: Trigger list matches BOD-167
- **WHEN** reading docs/OPENSPEC.md trigger list
- **THEN** all BOD-167 triggers are present

#### Scenario: Exemptions match BOD-167
- **WHEN** reading docs/OPENSPEC.md exemptions
- **THEN** small refactors and typo-only docs are exempt

### Requirement: Validator SHALL use stdlib only

The validator SHALL use only Python standard library to match existing script style
and avoid external dependencies.

#### Scenario: Validator has no external imports
- **WHEN** inspecting scripts/validate_openspec_change.py imports
- **THEN** all imports are from Python stdlib

### Requirement: Tests SHALL follow unittest style

Tests SHALL use unittest framework to match existing test suite style in
tests/test_spec_kit_lite.py.

#### Scenario: Test file uses unittest
- **WHEN** inspecting tests/test_openspec_change.py
- **THEN** test class inherits from unittest.TestCase
