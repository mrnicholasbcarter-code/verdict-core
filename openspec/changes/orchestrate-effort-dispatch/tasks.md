# Tasks

## 1. Selection

- [x] 1.1 Implement, review, and research selection events record the task-kind effort: `tests/test_orch_effort_dispatch.py`
- [x] 1.2 A reasoning-disabled row records no override, and a map without medium clamps down: `tests/test_orch_effort_dispatch.py`

## 2. Dispatch

- [x] 2.1 Direct gateway sends `reasoning_effort` only when a level is passed: `tests/test_orch_effort_dispatch.py`
- [x] 2.2 Prime argv uses the per-call effort and does not mutate constructor state: `tests/test_orch_effort_dispatch.py`
- [x] 2.3 An executor that does not accept `effort` still runs: `tests/test_orch_effort_dispatch.py`
- [x] 2.4 Mixed, answer-only, and fault wrappers forward the per-call value: `tests/test_orch_effort_dispatch.py`

## 3. Receipt

- [x] 3.1 The dispatched attempt carries chosen effort, and a replaced selection does not leak: `tests/test_orch_effort_dispatch.py`
- [x] 3.2 A legacy event stream without the field keeps the previous receipt shape: `tests/test_orch_effort_dispatch.py`

## 4. Spec

- [x] 4.1 `openspec validate orchestrate-effort-dispatch --strict`

- [ ] (post-merge) After install on a clean main checkout, one orchestrate attempt receipt shows `chosen_effort` and `executed_effort=unverified` without a live paid call.
