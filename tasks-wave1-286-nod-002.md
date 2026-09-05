# Wave 1 #286 NOD-002 Node Envelope Enforcement Tasks

## Phase 1: Setup & Investigation

- [x] T001 Read ADR-025 (docs/adr/ADR-025-node-envelope-enforcement.md) for current status and requirements
- [x] T002 Read CONTRACT_PARITY.md for existing parity verification approach
- [x] T003 Read verdict/contracts.py to audit ExecutionEnvelope.__post_init__ invariants
- [x] T004 Read verdict-node/packages/contracts/src/execution-envelope.ts for TypeScript implementation
- [ ] T005 Read verdict-ecosystem/VERDICT-NOD-002.md for story context — SKIPPED: file does not exist in verdict-ecosystem; story context taken from ADR-025

## Phase 2: Python Invariants Audit

- [x] T006 [US1] Extract all invariant checks from ExecutionEnvelope.__post_init__ in verdict/contracts.py
- [x] T007 [US1] Document each invariant: field, check logic, error message, test case
- [x] T008 [US1] Create invalid-envelope fixture set in tests/fixtures/invalid_envelopes.py covering each invariant violation

## Phase 3: TypeScript Port

- [x] T009 [US2] Add invariant checks to ExecutionEnvelope constructor/factory in verdict-node/packages/contracts/src/execution-envelope.ts
- [x] T010 [US2] Ensure TypeScript throws same error types/messages as Python for each invariant
- [x] T011 [US2] Add TypeScript tests for each invariant in verdict-node/packages/contracts/tests/execution-envelope.test.ts

## Phase 4: Cross-Runtime Parity Extension

- [x] T012 [US3] Extend CONTRACT_PARITY.md with shared invalid-envelope fixture table (Python fixture + TS fixture + expected rejection)
- [x] T013 [US3] Add parity test runner that loads fixtures and verifies both runtimes reject identically
- [x] T014 [US3] Ensure CONTRACT_PARITY.md test suite runs in both repos' CI

## Phase 5: CI Gate in verdict-node

- [x] T015 [US4] Add CI job in verdict-node/.github/workflows/ci.yml: "contract-parity"
- [x] T016 [US4] Job runs: Python pytest on invalid fixtures → must all reject; TypeScript tests on same fixtures → must all reject
- [x] T017 [US4] Job fails if TypeScript accepts an envelope Python rejects (or vice versa)
- [x] T018 [US4] Add intentional mismatch fixture to verify gate catches divergence

## Phase 6: ADR Update

- [x] T019 [US5] Update ADR-025 status from "Proposed" → "Accepted"
- [x] T020 [US5] Update ADR-025 Consequences section with actual implementation behavior
- [x] T021 [US5] Link ADR-002 to CONTRACT_PARITY.md and CI gate

## Phase 7: Verification

- [x] T022 Run Python tests: `uv run pytest tests/test_contracts.py -v`
- [x] T023 Run TypeScript tests: `cd /home/nick/dev/verdict-node && npm test`
- [ ] T024 Verify CI parity gate: push branch, confirm GitHub Actions "contract-parity" job passes — BLOCKED: verdict-core .git has core.bare=true (cannot commit/push); gate verified locally instead, incl. an induced-divergence failure test
- [x] T025 Verify ADR-025 shows "Accepted" status with updated consequences

## Verification Criteria

- ✅ All Python ExecutionEnvelope invariants ported to TypeScript
- ✅ CONTRACT_PARITY.md extended with shared invalid-envelope fixtures
- ✅ CI in verdict-node fails if TS accepts what Python rejects (or vice versa)
- ✅ ADR-025 updated from "Proposed" to "Accepted" with actual behavior consequences

## Dependencies

- Phase 1 (investigation) blocks all
- Phase 2 (Python audit) blocks Phase 3 (TS port)
- Phase 3 blocks Phase 4 (parity needs both implementations)
- Phase 4 blocks Phase 5 (CI gate needs parity test runner)
- Phase 5 blocks Phase 6 (ADR update needs implemented+verified)

## Cross-Repo Coordination

- verdict-core owns Python invariants (source of truth)
- verdict-node owns TypeScript port and CI gate
- @bodanglin/verdict-contracts package release needed after TS changes
- Coordinate with verdict-node owner for release timing

## Parallel Opportunities

- T001-T005 (investigation) fully parallel
- T006-T008 (Python audit) sequential
- T009-T011 (TS port) can start after T006-T008 complete
- T012-T014 (parity) after both implementations done
- T022-T023 (verification) parallel across repos