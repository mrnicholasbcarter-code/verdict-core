# Tasks — planner-repo-map

## 1. repo_map ordering and AGENTS.md section

- [x] 1.1 `repo_map()` sorts files by include-tuple order first, then
  alphabetically within each group. `verdict/` files appear before `tests/`
  files. Verify: `test_repo_map_lists_verdict_before_tests_in_include_order`.

- [x] 1.2 `_agents_md_where_things_live(root)` extracts the "Where things live"
  section from `root/AGENTS.md`, capped at 4 096 bytes. Returns `""` when the
  file is absent, unreadable, or lacks the section. Verify:
  `test_repo_map_includes_agents_md_where_things_live_section`,
  `test_repo_map_handles_missing_agents_md`,
  `test_repo_map_handles_agents_md_without_section`.

## 2. plan_context event and receipt

- [x] 2.1 `FrontierPlanner.plan()` emits `plan_context` with `repo_map_sha256`
  after computing the map, when `events` is not None. `plan_context` is added
  to `EVENT_TYPES` in `contracts.py`. Verify:
  `test_frontier_planner_emits_plan_context_with_repo_map_sha256`,
  `test_frontier_planner_no_plan_context_without_events`.

- [x] 2.2 `receipt.py` includes `plan_context` events in `planner_attempts`.
  Verify: `tests/test_orch_receipt.py` (60 tests pass).

## 3. Docs

- [x] 3.1 `docs/CLI_REFERENCE.md` updated to note that the repo structure map
  is automatic; `--constraints-file` remains for additional operator guidance.
  Verify: `uv run python scripts/check_doc_links.py` and
  `uv run python scripts/check_reachability.py` pass.

## 4. Verification

- [x] 4.1 Full test suite passes (`tests/test_orch_planner.py` 103 tests,
  `tests/test_orch_receipt.py` 60 tests,
  `tests/test_planner_confirmation_fallback.py` 13 tests). Verify: `uv run
  python -m pytest -n 4 tests/` green.
- [x] 4.2 `ruff check`, `ruff format --check`, `mypy --strict` green. Verify:
  each command exits 0.
- [x] 4.3 Reachability and lint gates green. Verify:
  `uv run python scripts/check_lint_gates.py` passes.

- [ ] (post-merge) 5.1 On installed main, an `orchestrate` run for a
  CLI-adding change plans owned files under `verdict/commands/`. Its receipt
  shows `repo_map_sha256` in `planner_attempts`, with no `--constraints-file`
  passed.
