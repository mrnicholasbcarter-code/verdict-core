---
description: "Task list for live routing golden path"
---

# Tasks: Live Routing Golden Path

**Input**: Design documents from `/specs/276-live-routing-golden-path/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. Each user story has Independent Test criteria in spec.md. TDD: write failing tests first.

**Repository**: `verdict-core` only. Isolated worktree at implement time. Default live gateway: `http://localhost:20128/v1`.

**Organization**: Tasks are grouped by user story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths

## Path Conventions

Paths are relative to the `verdict-core` worktree root.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Isolated worktree and golden-path module stubs

- [X] T001 Create isolated `verdict-core` worktree for `276-live-routing-golden-path` and add empty `verdict/golden_path.py` plus `verdict/golden_path_live.py`
- [X] T002 [P] Add `tests/test_golden_path_classify.py` and `tests/test_golden_path_live.py` as empty modules or failing placeholders; live tests MUST NOT skip or xfail as success

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Entities, contract errors, and stop name-guessing before any story

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 Add Gateway, Provider, ConcreteIdentity, Mix, Candidate dataclasses and unclassified/stale/opaque rules in `verdict/golden_path.py`
- [X] T004 Remove name-heuristic capability defaults (`claude`/`gpt-4`/`vision` guesses and `tools=True` defaults) from `verdict/catalog.py`; missing fetched specs stay unclassified
- [X] T005 Map `golden-path/v1` stable error codes (`empty_catalog`, `unclassified`, `stale_specs`, `opaque_mix`, `no_qualified_candidate`, `checker_failed`, `exhausted`, `live_surface_blocked`) in `verdict/golden_path.py`
- [X] T006 Reuse capture/freshness helpers from `verdict/omniroute_catalog.py` (default window 3600s) without treating catalog membership as qualification

**Checkpoint**: Foundation ready — unclassified-if-missing-specs; no name guesses

---

## Phase 3: User Story 1 - Discover and filter available models (Priority: P1)

**Goal**: Fetch identities, classify from published specs, drop denied/unhealthy/unclassified/stale/opaque with reasons.

**Independent Test**: Mixed catalog includes denied, unhealthy, unclassified, qualified. Only qualified healthy remain; each drop has a reason.

### Tests for User Story 1

- [X] T007 [P] [US1] Fail-first classify tests (denied, unclassified, stale, name-heuristic must not qualify) in `tests/test_golden_path_classify.py`
- [X] T008 [P] [US1] Fail-first live catalog fetch test that uses `GET /v1/models` in `tests/test_golden_path_live.py` (unreachable → `live_surface_blocked`, not pass)

### Implementation for User Story 1

- [X] T009 [US1] Implement live catalog fetch client in `verdict/golden_path_live.py` (`GET /v1/models`; if cost class is missing on a row, fetch gateway pricing for the same capture; still missing → unclassified)
- [X] T010 [US1] Classify fetched rows into kept/dropped candidates with reasons in `verdict/golden_path.py` (operator denylist supplies live “denied”)
- [X] T010a [US1] Probe a bounded live sample for health/capability in `verdict/golden_path_live.py`; unknown or failed probe MUST NOT be kept
- [X] T010b [P] [US1] First-party usage probes in `verdict/golden_path_usage.py`: discover well-known credential JSON/env per provider (no CodexBar/Toolbar dependency, no cookie scrape, no secret persistence), fetch that provider’s usage endpoint, map remaining quota; exhausted quota cannot stay cheaper/free
- [X] T011 [US1] Drop `auto/*`, unexpanded aliases, and unnamed mix steps during discovery in `verdict/golden_path.py`

**Checkpoint**: Live listing classified; fixtures only for rule tests; name heuristics fail tests

---

## Phase 4: User Story 2 - Prefer cheaper qualified routes over paid (Priority: P1)

**Goal**: Never select paid while a cheaper/free/local kept candidate exists. Mix cost class is first remaining qualified step.

**Independent Test**: Qualified cheaper + qualified paid → paid unused; explanation says cheaper existed.

### Tests for User Story 2

- [X] T012 [P] [US2] Fail-first cheaper-first selection tests (paid illegal while cheaper kept; mix paid-first not cheaper; rank local then free then cheaper then paid; same catalog twice is deterministic) in `tests/test_golden_path_classify.py`

### Implementation for User Story 2

- [X] T013 [US2] Implement cheaper-first selection (rank local, free, cheaper, paid; lexical identity_id ties) and mix cost-class-from-first-step in `verdict/golden_path.py`
- [X] T014 [US2] Record `paid_used` and `cheaper_available` on RouteSelection; reject the illegal pair in `verdict/golden_path.py`

**Checkpoint**: Paid cannot win while cheaper kept remains

---

## Phase 5: User Story 3 - Explain keep, drop, and spend decisions (Priority: P1)

**Goal**: Non-developer explanation of kept, dropped, why, and whether paid was used. No secrets.

**Independent Test**: Reviewer with only the explanation names chosen model, one drop reason, and paid/not paid.

### Tests for User Story 3

- [X] T015 [P] [US3] Fail-first explanation tests (drop reasons, cheaper-vs-paid, no secret fields) in `tests/test_golden_path_classify.py`

### Implementation for User Story 3

- [X] T016 [US3] Emit allowlisted explanation payload in `verdict/golden_path.py`
- [X] T017 [US3] Strip secrets, prompts, completions, and raw tool arguments from explanation in `verdict/golden_path.py`

**Checkpoint**: Explanation is the operator-visible product, not logs

---

## Phase 6: User Story 4 - One bounded execution with evidence (Priority: P1)

**Goal**: Named pre-stated check on the selected live identity; cheaper-first failover including paid after cheaper unused identities are gone; receipt.

**Independent Test**: Live selected identity runs the named check; checker pass/fail; receipt lists attempts.

### Tests for User Story 4

- [X] T018 [P] [US4] Fail-first named-check contract tests (unchecked reply is not success; same-identity retry forbidden) in `tests/test_golden_path_classify.py`
- [X] T019 [US4] Fail-first live execute test: selected identity must return parseable `{"golden_path":"ok"}` and fail on any other reply in `tests/test_golden_path_live.py`

### Implementation for User Story 4

- [X] T020 [US4] Execute named check through `verdict/golden_path_live.py` on the selected identity
- [X] T021 [US4] Fail over unique remaining qualified identities cheaper-first then paid; no same-identity retry; exhaust → fail closed in `verdict/golden_path.py`
- [X] T022 [US4] Write allowlisted receipt (endpoint, identity, attempts, cheaper-vs-paid, checker) using existing receipt surfaces in `verdict/golden_path.py`

**Checkpoint**: Real execution, not a catalog listing

---

## Phase 7: User Story 5 - Live gateway required (Priority: P1)

**Goal**: Golden-path pass requires live fetch + live execute. Unreachable surface is blocked, not passed. Fixtures cannot emit a pass receipt.

**Independent Test**: Live gateway: fetch, select, execute, receipt names endpoint. Gateway down: `live_surface_blocked`, not success.

### Tests for User Story 5

- [X] T023 [P] [US5] Fail-first: fixture-only orchestrator path must not emit golden-path pass in `tests/test_golden_path_classify.py`
- [X] T024 [US5] Fail-first: unreachable `gateway_base_url` returns `live_surface_blocked` and fails the live job in `tests/test_golden_path_live.py`

### Implementation for User Story 5

- [X] T025 [US5] Require `catalog_source=live-gateway` for demonstration receipt in `verdict/golden_path.py`
- [X] T026 [US5] Map down/timeout/auth-fail of the live client to `live_surface_blocked` in `verdict/golden_path_live.py`

**Checkpoint**: No vaporware pass

---

## Phase 8: User Story 6 - Cookie/browser usage probes (Priority: P3, later)

**Goal**: Opt-in CodexBar-style cookie strategies after file+API usage works. Not required for the golden-path demo.

**Independent Test**: Default off = no cookie reads. Opt-in + cookie present = allowlisted quota. Failure skips that provider. Secrets never on receipts.

- [ ] T027 [US6] Document later-phase cookie probe contract (domains, opt-in flag, no persistence) in `docs/guides/golden-path.md`
- [ ] T028 [US6] Implement opt-in cookie usage probes (Cursor / Claude web extras / Copilot budget extras) in `verdict/golden_path_usage.py` only after US1–US5 pass
- [ ] T029 [P] [US6] Tests that default is no cookie I/O and exhausted cookie quota cannot stay cheaper in `tests/test_golden_path_classify.py`

**Checkpoint**: Later; do not start until US5 live demo exists

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Docs and repo-native verification

- [X] T030 [P] Document live golden-path run and blocked-if-down behavior in `docs/guides/golden-path.md` (or extend an existing verdict-core guide if one already covers routing)
- [X] T031 Run `uv run pytest -q tests/test_golden_path_classify.py tests/test_golden_path_live.py` plus `uv run ruff check` on touched files in the worktree
- [X] T032 Execute `specs/276-live-routing-golden-path/quickstart.md` against the real gateway and record pass or `live_surface_blocked` without converting block into pass

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **US1**: After Phase 2
- **US2**: After US1 classify/kept-set (same `verdict/golden_path.py`)
- **US3**: After US2 selection fields
- **US4**: After US2 + live client from US1
- **US5**: Can start tests after Phase 2; wire after US1 live client
- **US6 (later)**: After US5 live demo
- **Polish**: After US4 and US5; US6 is optional and later

### User Story Dependencies

- **US1**: After Phase 2
- **US2**: Needs US1 Candidate keep/drop
- **US3**: Needs US2 RouteSelection
- **US4**: Needs US2 + live execute client
- **US5**: Tightens US1/US4 into the only allowed pass path
- **US6 (P3 later)**: Cookie probes; must not block US1–US5

### Parallel Opportunities

- T002 stubs parallel with T001 after worktree exists
- T007 and T008 after Phase 2
- T012 and T015 after US1 keep/drop exists (T015 needs selection; wait for T013)
- T018 classify vs T019 live (different files)

---

## Parallel Example: User Story 1

```bash
Task: "Fail-first classify tests in tests/test_golden_path_classify.py"
Task: "Fail-first live catalog fetch in tests/test_golden_path_live.py"
```

---

## Implementation Strategy

### MVP (not a fixture demo)

1. Phase 1 + Phase 2
2. US1 live fetch + classify
3. US5 blocked-if-down
4. **STOP**: If live gateway is down, the feature is blocked — do not ship a fixture pass
5. Then US2 cheaper-first, US3 explanation, US4 live named check

### Incremental Delivery

1. Setup + foundation (stop name-guessing)
2. US1 + US5 — live catalog truth
3. US2 — paid-usage
4. US3 — explanation
5. US4 — live named check + receipt
6. Polish / quickstart against the real gateway

---

## Notes

- [P] = different files, no incomplete dependency
- Independent Tests in spec.md are the story gates
- Live tests must not treat skip/xfail as pass
- Do not commit secrets
- Next command after this file: `/speckit-analyze`, then `/speckit-implement` only after `checklists/golden-path.md` is reviewer-checked

## Phase 10: Convergence

- [X] T033 Remove the 3-attempt execute cap in `verdict/live_routing_run.py` so failover walks remaining unique qualified identities cheaper-first including paid after cheaper unused identities are gone, per FR-011a (partial)
- [X] T034 Add xAI management prepaid-balance usage probe (management key + team id) in `verdict/live_routing_usage.py` per FR-001h / plan usage table (partial)
- [X] T035 Set mix cost class from the first remaining qualified mix step, not always `steps[0]`, in `verdict/live_routing.py` per FR-001e (partial)
