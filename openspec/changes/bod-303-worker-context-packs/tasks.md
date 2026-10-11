# Tasks

## 1. Wire the first worker context slice

Owned files: `verdict/orchestration/planner.py`, `verdict/orchestration/worker_context.py`, `tests/test_orch_worker_context.py`. Verification: `python -m pytest -q tests/test_orch_planner.py tests/test_orch_worker_context.py` with the project environment. Dependencies: existing Core APIs only.

- [x] 1.1 Read AGENTS, ADR-0001/022/036 and source contracts.
- [x] 1.2 Add a native adapter and wire production planner hydration; preserve ownership/verification rules.
- [x] 1.3 Add bounded source citations, optional candidate plan and honest conservative provenance.
- [x] 1.4 Add provider/query fallback, secret/path/transcript exclusions and no-memory behavior.

## 2. Validate the increment

Depends on group 1. Owned artifacts: this change directory and `docs/BOD-303-WORKER-CONTEXT.md`. Verify with focused tests and scoped `ruff check`, `ruff format --check`, `mypy --strict`, `python verdict/openspec_vendor/validate_openspec_change.py openspec/changes/bod-303-worker-context-packs`, `python scripts/check_doc_links.py`, and `python scripts/check_reachability.py --skip-vulture --json`.

- [x] 2.1 Run focused planner/adapter regressions including scripted runtime dispatch.
- [x] 2.2 Run scoped ruff and strict mypy, doc links, reachability and OpenSpec contract validation.
- [x] 2.3 Commit and report evidence/limits. Do not claim live self-hosting or whole BOD-303 complete.

## 3. Named later increment (not implemented here)

Coordinator-owned follow-up scope; depends on group 2 and separate runtime/run admission. Verification must use focused runtime regressions and authorized live receipts. No live verification is performed in this slice.

- [ ] 3.1 Coordinator forwards selected candidate ContextPlan through runtime/run hydration.
- [ ] 3.2 Coordinator records actual context pack sources and budgets in hydrate events.
- [ ] 3.3 Obtain authorized live proof after bootstrap blockers are repaired.
