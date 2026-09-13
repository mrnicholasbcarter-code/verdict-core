# Feature Specification: Python/TypeScript Contract Parity (BOD-12)

**Feature Branch**: `feat/bod-12-contract-parity`
**Created**: 2026-09-13
**Status**: Draft
**Linear**: BOD-12

## User Scenario

As a maintainer of Python and Node consumers, I need decision, execution-envelope, provider-receipt, and proof-reference payloads to round-trip under equivalent strict schemas so cross-runtime consumers cannot silently disagree.

## Acceptance Criteria

- Mirrored JSON schemas remain byte-identical.
- Python and TypeScript default/null semantics match for the shared contracts.
- Shared round-trip fixtures cover routing decision, execution envelope, provider receipt, and proof reference.
- Unknown fields are rejected according to the documented strict compatibility policy.
- Existing TypeScript parity CI remains green.

## Requirements

- Reuse canonical `schemas/contracts.v1.json`, packaged mirror `verdict/schemas/contracts.v1.json`, and `contracts/src/index.ts`.
- Add only missing schema/type/fixture/test coverage. Do not redesign contracts or make breaking changes.
- Review ADR-025 and preserve the NOD-002 enforcement boundary.
- No new ADR is required if work only proves existing accepted contracts. A new ADR is required before any breaking field or compatibility-policy change.

## Proof Contract

- STATIC: Ruff/format/mypy on touched Python and TypeScript typecheck/build.
- UNIT: Python schema/contract tests and TypeScript contract tests.
- INTEGRATION: shared fixtures parsed and serialized by both runtimes.
- ACCEPTANCE-PROOF: `diff -q schemas/contracts.v1.json verdict/schemas/contracts.v1.json`, four shared fixture kinds, strict unknown-field rejection, green CI.
