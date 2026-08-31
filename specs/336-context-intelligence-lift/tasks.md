---
description: "Task list for context intelligence lift"
---

# Tasks: Context Intelligence Lift

**Input**: Design documents from `/specs/336-context-intelligence-lift/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. TDD: write failing tests first. Live proof is required for US3; fixture-only is not a pass.

**Repository**: `verdict-core` only. Isolated worktree `feat/272-p3-context`. Default live gateway: `http://localhost:20128/v1`.

**Organization**: Tasks are grouped by user story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths

## Path Conventions

Paths are relative to the `verdict-core` worktree root.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Module stubs for the context intelligence plane and paired lift

- [x] T001 Add empty `verdict/context_intelligence.py` and `verdict/context_lift.py` per implementation plan
- [x] T002 [P] Add `tests/test_context_intelligence.py`, `tests/test_context_lift.py`, and `tests/test_context_lift_live.py` as failing placeholders; live tests MUST NOT skip or xfail as success

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Errors, entities, and reuse of pack/memory/live routing before any story

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Add `ContextIntelligenceError` stable codes (`live_surface_blocked`, `no_cheaper_identity`, `unclassified_context_limit`, `required_fact_missing`, `required_fact_omitted`, `repo_dump_refused`, `secret_refused`, `invalid_pair`) in `verdict/context_intelligence.py`
- [x] T004 [P] Add `RetrievalSlice`, `Omission`, and `WorkingState` dataclasses in `verdict/context_intelligence.py`
- [x] T005 [P] Add `LiftReceipt` allowlisted fields and secret stripping in `verdict/context_lift.py`
- [x] T006 Reuse `ContextPackCompiler`, `MemoryPlane`, `MemoryGate`, `documentation_preflight.discover_sources`, `CodeGraphEngine`, and `verdict.live_routing` select without adding a vendor SDK

**Checkpoint**: Foundation ready — entities and fail-closed codes exist; no vendor dependency

---

## Phase 3: User Story 1 - Plan slices and retrieve (Priority: P1) 🎯 MVP

**Goal**: Deterministic slices retrieve planted docs/code/memory units without dumping the repository.

**Independent Test**: Plant a unique fact in docs, code, and memory among 20+ unrelated files. Retrieval includes planted units and excludes at least 90% of files.

### Tests for User Story 1

- [x] T007 [P] [US1] Fail-first retrieval tests (planted hits, repo dump refused, omissions named, 20-file exclusion) in `tests/test_context_intelligence.py`

### Implementation for User Story 1

- [x] T008 [US1] Implement `plan_slices` (docs/adr default, bounded code query, memory query; refuse repo-root dump) in `verdict/context_intelligence.py`
- [x] T009 [US1] Implement `retrieve_units` using local docs, bounded `CodeGraphEngine`/file match, and `MemoryPlane.search_ranked` in `verdict/context_intelligence.py`
- [x] T010 [US1] Record omissions for missing categories and attach provenance on each `ContextUnit` in `verdict/context_intelligence.py`

**Checkpoint**: US1 independently testable with planted sources

---

## Phase 4: User Story 2 - Compile model-aware pack (Priority: P1)

**Goal**: Budgeted typed-slot pack; required fact retained; secrets excluded; conflicts recorded.

**Independent Test**: Compile the same units for a small and large budget. Small pack stays in budget, keeps the unique fact, records drops; over-small budget refuses.

### Tests for User Story 2

- [x] T011 [P] [US2] Fail-first compile tests (budget, required-fact refuse, secret exclude, conflict record, receipt has no secrets) in `tests/test_context_intelligence.py`

### Implementation for User Story 2

- [x] T012 [US2] Implement `compile_pack` wrapping `ContextPackCompiler.compile_units` with required-fact retention in `verdict/context_intelligence.py`
- [x] T013 [US2] Fail closed when policy/required fact cannot fit; map docs/code/memory into typed slots in `verdict/context_intelligence.py`
- [x] T014 [US2] Emit `WorkingState` typed slots (not a transcript) and a payload-free pack receipt in `verdict/context_intelligence.py`

**Checkpoint**: US2 independently testable offline

---

## Phase 5: User Story 3 - Paired live cheaper-model lift (Priority: P1)

**Goal**: Same cheaper live identity runs the named check unaided then packed; receipt reports lift/no_lift/blocked.

**Independent Test**: On a reachable live gateway, select a cheaper identity, run both attempts, record conclusion. Down gateway → blocked, not passed.

### Tests for User Story 3

- [x] T015 [P] [US3] Fail-first pairing/checker tests (exact JSON, token absent from unaided prompt, invalid pair, blocked vs lift) in `tests/test_context_lift.py`
- [x] T016 [P] [US3] Fail-first live paired test in `tests/test_context_lift_live.py` (unreachable → `live_surface_blocked`, not pass)

### Implementation for User Story 3

- [x] T017 [US3] Implement planted-token workspace helper and `lift_check_passes` in `verdict/context_lift.py`
- [x] T018 [US3] Select cheaper-first live identity via `verdict/live_routing.py`; block paid-as-subject and unknown context limit in `verdict/context_lift.py`
- [x] T019 [US3] Execute unaided then packed on the same identity; emit `LiftReceipt` in `verdict/context_lift.py`
- [x] T020 [US3] Add generic chat-completions helper for custom messages without changing 276’s golden-path prompt in `verdict/live_routing_gateway.py`

**Checkpoint**: US3 live proof runnable; fixture cannot emit lift

---

## Phase 6: User Story 4 - Working state vs durable memory (Priority: P2)

**Goal**: Gated ingest/search; secrets refused; working state not auto-archived.

**Independent Test**: Ingest a unique fact through the gate; new working state retrieves it. Secret write never appears in search or pack.

### Tests for User Story 4

- [x] T021 [P] [US4] Fail-first gate/search tests (ingest, secret refuse, working state not auto-ingested, adapter absence still works) in `tests/test_context_lift.py`

### Implementation for User Story 4

- [x] T022 [US4] Implement gated plant/search helpers using `MemoryGate` + `MemoryPlane` in `verdict/context_lift.py`
- [x] T023 [US4] Ensure `WorkingState` is per-attempt and `run_context_lift` does not ingest it automatically in `verdict/context_lift.py`

**Checkpoint**: US4 independently testable offline

---

## Phase 7: User Story 5 - Optional compaction (Priority: P3)

**Goal**: Surplus units omitted or extractively shortened; required fact verbatim.

**Independent Test**: Over-budget units with compaction off list omissions; with compaction on, required fact remains verbatim.

### Tests for User Story 5

- [x] T024 [P] [US5] Fail-first compaction tests in `tests/test_context_intelligence.py`

### Implementation for User Story 5

- [x] T025 [US5] Implement optional extractive compaction that never replaces the required fact in `verdict/context_intelligence.py`

**Checkpoint**: Compaction optional and inspectable

---

## Phase 8: Polish & Cross-Cutting Concerns

- [x] T026 [P] Match `specs/336-context-intelligence-lift/quickstart.md` commands to the implemented modules
- [x] T027 Run `uv run pytest -q tests/test_context_intelligence.py tests/test_context_lift.py tests/test_context_lift_live.py` plus `ruff check` on touched files

---

## Dependencies & Execution Order

- Setup → Foundational (blocks stories) → US1 → US2 → US3 (live) → US4 → US5 → Polish
- US1/US2 are the packing MVP; US3 is the live exit signal
- Tests fail first within each story

## Parallel opportunities

- T002/T004/T005; T007; T011; T015/T016; T021; T024

## Implementation strategy

MVP: T001–T014 (retrieve + compile). Exit: T015–T020 (paired live lift).
