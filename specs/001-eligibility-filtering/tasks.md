---

description: "Task list for 001-eligibility-filtering"
---

# Tasks: Eligibility Filtering

**Input**: Design documents from `specs/001-eligibility-filtering/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, quickstart.md (no contracts/ — internal module extension)

**Tests**: Not explicitly requested in the spec; existing `tests/test_eligibility_gate.py` suite is extended per story instead of adding a separate contract-test layer.

**Scope note (from plan.md)**: `EligibilityGate`/`EligibilityRecord`/`EligibilityResult` already exist and are fail-closed. This feature adds one field (`confidence: float`) and wires the three clarified behaviors (FR-003 confidence mapping, FR-006 re-check-before-selection, FR-007 per-request-type flag) into the existing single `evaluate()` path — it is a small additive extension, not new build.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1/US2/US3)

---

## Phase 1: Setup

No new setup required — extends the existing `verdict/eligibility.py` module in place, no new dependencies (per research.md).

- [X] T001 Re-run `uv run pytest tests/test_eligibility_gate.py -q` to confirm the existing eligibility baseline is green before changing `EligibilityRecord`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The `confidence` field is shared by all three user stories — it must exist before US1–US3 can be implemented.

- [X] T002 Add `confidence: float` field to `EligibilityRecord` in `verdict/eligibility.py` (near line 44, alongside existing `reason: str | None`); update its `to_dict()`/serialization method to include `confidence`
- [X] T003 Implement the confidence-score mapping from research.md in `verdict/eligibility.py`'s `_judge` method: `eligible` (including the `READY` enum alias) → `1.0`, `degraded` → `0.5`, all other states (`unknown`/`error`/excluded) → `0.0`

**Checkpoint**: `EligibilityRecord` now carries a confidence score on every evaluation — user story work can begin.

---

## Phase 3: User Story 1 - Protect routed work from ineligible candidates (Priority: P1) 🎯 MVP

**Goal**: FR-003 fail-closed behavior and FR-006 re-check-before-final-selection are both explicit and verified, using the new confidence field.

**Independent Test**: Call `EligibilityGate.evaluate()` for a protected candidate whose confidence resolves to `0.0` and assert it is excluded; call it a second time simulating a state change between evaluation and final selection and assert the later (authoritative) result wins.

- [X] T004 [US1] In `verdict/eligibility.py`, confirm/adjust `_judge`'s protected fail-closed branches (~lines 159, 193) to key off the new confidence score (confidence `0.0` ⇒ exclude when `protected=True`), not just raw state string matching
- [X] T005 [US1] In `verdict/decision_kernel.py`, verify the single synchronous `evaluate()` call site is invoked immediately before final candidate selection (not earlier in the pipeline) so FR-006's re-check requirement holds; add a code comment noting this is the FR-006 re-check point if not already documented
- [X] T006 [US1] Add/extend tests in `tests/test_eligibility_gate.py` for: confidence mapping (eligible/degraded/unknown/denied → 1.0/0.5/0.0/0.0), protected fail-closed exclusion driven by confidence, and a late-transition case (candidate eligible at an earlier check, ineligible at the evaluate() call used for final selection) confirming the later result is authoritative

**Checkpoint**: US1 independently testable — ineligible/low-confidence candidates never reach final selection, including late transitions.

---

## Phase 4: User Story 2 - Explain eligibility decisions (Priority: P2)

**Goal**: FR-007's per-request-type "best-available" posture and the confidence score are visible in the routing explanation, not just internal state.

**Independent Test**: Evaluate a non-protected candidate with `dev_mode=True` (the reused FR-007 flag) and low confidence; assert the resulting `EligibilityRecord`'s `reason` text and `confidence` field together make the "admitted on best-available info" posture visible.

- [X] T007 [US2] In `verdict/eligibility.py`, update the `dev_mode`-admits-unverified branch (~line 213) so its `reason` string explicitly states the confidence score and that the candidate was admitted under best-available/degraded information (FR-007 visibility requirement)
- [X] T008 [US2] Add tests in `tests/test_eligibility_gate.py` asserting the `reason` text and `confidence` value are both present and correct for the FR-007 best-available admission path

**Checkpoint**: US1 + US2 both independently functional — decisions and their confidence/posture are inspectable via `EligibilityRecord`.

---

## Phase 5: User Story 3 - Consistent eligibility across route entry points (Priority: P3)

**Goal**: Confirm there is exactly one eligibility evaluation path so behavior can't diverge between entry points.

**Independent Test**: Grep/verify all routing entry points that select a candidate call the same `EligibilityGate.evaluate()`; a test asserting two different entry points produce identical `EligibilityRecord.confidence`/status for the same candidate input proves consistency.

- [X] T009 [US3] Verify (via `grep -rn "EligibilityGate\|\.evaluate(" verdict/`) that all routing entry points use the shared `EligibilityGate` authority before ranking; document that context-specific gate instances are intentional where a second call site is required
- [X] T010 [US3] Add a test in `tests/test_eligibility_gate.py` exercising two distinct entry points with the same candidate input and asserting identical eligibility output

**T009 evidence (2026-09-05)**: `decision_kernel.py`, `intelligence.py`, `subagent_models.py`, and `autodev_routing.py` each apply the same `EligibilityGate` policy before ranking, while `api.py` applies it only for explainability. Separate gate instances are intentional because each surface owns a different authoritative availability source; consolidating to one physical call site would break the pure decision-kernel, per-report subagent, and per-request autodev contracts. Advisory ranking remains restricted to the admitted set at each routing surface.

**Checkpoint**: All three user stories independently functional.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T011 [P] Run `specs/001-eligibility-filtering/quickstart.md` validation scenarios end-to-end
- [ ] T012 Run full baseline: `uv run pytest -q`, `ruff check .`, `ruff format --check .`, `mypy verdict --strict` per CLAUDE.md verification section
- [ ] T013 Update `specs/001-eligibility-filtering/spec.md` Status field from Draft to Implemented (or per project convention) once T001–T012 are green

**Phase 6 evidence (2026-09-05)**: T011 quickstart tests and manual scenarios passed; the full test suite passed (`1348 passed, 1 warning`), strict mypy passed (`108 source files`), the feature-scoped Ruff checks passed, the package build passed, and `git diff --check` passed. T012 remains open because the required repository-wide `ruff check .` and `ruff format --check .` are still blocked by unrelated pre-existing files under `.specify/`, `.worktrees/`, and `test_fixtures/envelopes/test_envelope_parity.py`; no unrelated files were changed. T013 remains open while T012 is not fully green, so `spec.md` correctly remains `Draft`.

---

## Dependencies & Execution Order

- **Setup (Phase 1)**: No dependencies
- **Foundational (Phase 2)**: Depends on Setup; blocks all user stories (adds the shared `confidence` field)
- **US1 (P1)**: Depends on Phase 2 only
- **US2 (P2)**: Depends on Phase 2 only; reuses US1's confidence mapping but is independently testable
- **US3 (P3)**: Depends on Phase 2 only; independently testable
- **Polish (Phase 6)**: Depends on all desired stories being complete

## Parallel Opportunities

- T002 and T003 are sequential (same file, same method) — not parallel
- Once Phase 2 completes, US1/US2/US3 test-writing tasks (T006, T008, T010) can proceed in parallel since they touch different assertions, though all three currently land in `tests/test_eligibility_gate.py` — coordinate to avoid merge conflicts on that single file, or split into separate test functions added independently
- T011 [P] can run alongside T012

## Implementation Strategy

**MVP = User Story 1 only** (T001–T006): the fail-closed guarantee is the feature's core safety property; US2 (explainability) and US3 (consistency check) are additive on top of it.
