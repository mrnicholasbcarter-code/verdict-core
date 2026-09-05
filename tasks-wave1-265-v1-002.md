# Wave 1 #265 V1-002 Route Selection Tasks

## Phase 1: Setup & Investigation

- [ ] T001 Read WAVE0-PROGRESS.md §#265 criterion 3 to understand IntelligenceService discovery cache trace
- [ ] T002 Read verdict/gate.py to understand Gate.__init__ and Gate.route() current implementation
- [ ] T003 Read verdict/intelligence/service.py to understand IntelligenceService.__init__ and route() method
- [ ] T004 Read verdict/cli.py to understand cmd_route current implementation
- [ ] T005 Read issue #265 acceptance criteria: deterministic Strategy record, DIRECT-frontier comparison, allow_offline=true

## Phase 2: Foundational — allow_offline Plumbing

- [ ] T006 [US1] Add `allow_offline: bool = False` parameter to `Gate.__init__` in verdict/gate.py
- [ ] T007 [US1] Pass `allow_offline` to `IntelligenceService` constructor in Gate.__init__
- [ ] T008 [US1] Add `allow_offline: bool = False` parameter to `IntelligenceService.__init__` in verdict/intelligence/service.py
- [ ] T009 [US1] Store `allow_offline` as instance attribute in IntelligenceService
- [ ] T010 [US1] Modify `IntelligenceService.route()` to use static catalog when `allow_offline=True` (no network probes/discovery)

## Phase 3: Strategy Selection Record

- [ ] T011 [US2] Define `StrategySelection` dataclass in verdict/gate.py or verdict/types.py with fields: strategy (DIRECT|SWARM_AUTODEV), model, reasoning, timestamp
- [ ] T012 [US2] Modify `Gate.route()` to return `StrategySelection` record alongside route result
- [ ] T013 [US2] Update `cmd_route` in verdict/cli.py to emit StrategySelection record (JSON or structured output)

## Phase 4: DIRECT-Frontier Comparison Harness

- [ ] T014 [US3] Create `verdict/comparison.py` with `ComparisonHarness` class
- [ ] T015 [US3] Implement `run_direct(task: str) -> DirectResult` calling OmniRoute DIRECT API
- [ ] T016 [US3] Implement `run_verdict(task: str) -> VerdictResult` calling Gate.route()
- [ ] T017 [US3] Implement `compare(task: str) -> ComparisonReport` with deterministic fields: task, direct_model, verdict_strategy, verdict_model, latency_delta, cost_delta, quality_score
- [ ] T018 [US3] Add CLI command `verdict compare "task"` in verdict/cli.py

## Phase 5: Tests

- [ ] T019 [US4] Add test `tests/test_gate.py::test_allow_offline_static_catalog` — verify no network calls when allow_offline=True
- [ ] T020 [US4] Add test `tests/test_gate.py::test_strategy_selection_record` — verify StrategySelection emitted with correct fields
- [ ] T021 [US4] Add test `tests/test_intelligence.py::test_route_offline_mode` — verify IntelligenceService uses static catalog offline
- [ ] T022 [US4] Add test `tests/test_cli_smoke.py::test_cmd_route_emits_strategy` — verify CLI emits StrategySelection
- [ ] T023 [US4] Add test `tests/test_comparison.py::test_direct_vs_verdict_deterministic` — verify comparison report deterministic

## Phase 6: Verification & Gates

- [ ] T024 Run pre-commit: `uv run --extra dev ruff check . && uv run --extra dev ruff format --check . && uv run --extra dev mypy verdict --strict`
- [ ] T025 Run pre-push: `uv run pytest -q` (ignore test_vcr_fallback.py)
- [ ] T026 Verify: `verdict route --allow-offline "test task"` emits StrategySelection record
- [ ] T027 Verify: `verdict compare "test task"` produces deterministic ComparisonReport

## Verification Criteria

- ✅ cmd_route emits StrategySelection record (DIRECT vs SWARM_AUTODEV)
- ✅ Deterministic comparison report: DIRECT API model vs Verdict route on same task
- ✅ v1 readable without network when allow_offline=true (all pre-commit + pre-push green)

## Dependencies

- Phase 2 (allow_offline) blocks Phase 3 (Strategy record needs offline path)
- Phase 3 blocks Phase 4 (comparison harness uses StrategySelection)
- Phase 4 blocks Phase 5 (tests need implemented features)
- All phases must pass Phase 6 gates

## Parallel Opportunities

- T001-T005 (investigation) fully parallel
- T006-T010 (allow_offline plumbing) sequential within phase
- T011-T013 (StrategySelection) can run in parallel with T014-T017 (comparison harness) after Phase 2
- T019-T023 (tests) fully parallel once implementation complete