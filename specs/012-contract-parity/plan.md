# Implementation Plan: BOD-12 Contract Parity

**Branch**: `feat/bod-12-contract-parity`
**Worktree**: `/home/nick/dev/.worktrees/bod-12-contract-parity`
**Base**: `d3f43e7` (`origin/main`)

## Capability/ADR Decision

- Code Review Graph and Serena had no usable graph for this worktree, so exact deterministic file inspection is the named fallback.
- ADR-025 is accepted and already owns the Python/TypeScript envelope enforcement boundary.
- Decision: EXTEND tests and missing TypeScript schema/types. Do not create a new ADR unless the worker finds a breaking contract change.

## Existing Seam

- `schemas/contracts.v1.json` and `verdict/schemas/contracts.v1.json` are already identical.
- `contracts/src/index.ts` has strict Zod schemas and `parseContract`/`serializeContract`.
- Existing tests cover execution-envelope strictness; provider/proof receipt coverage is mainly Python.

## Implementation

1. Identify exact existing canonical definitions for routing decision, execution envelope, provider receipt, and proof reference.
2. Add minimal missing TypeScript Zod schemas/types and shared fixtures without altering existing field semantics.
3. Add Python and TypeScript tests that load the same fixture files, round-trip each kind, and reject unknown fields.
4. Keep both JSON schema copies identical.
5. Run proof commands from repository-declared scripts.

## Proof Commands

- `diff -q schemas/contracts.v1.json verdict/schemas/contracts.v1.json`
- `uv run --extra dev pytest -q tests/test_contracts.py tests/test_envelope_parity.py tests/test_provider_receipts.py tests/test_proof_receipts.py`
- `npm ci --workspaces`
- `cd contracts && npm run build && npm test`
- `npm run build`
- `git diff --check`
