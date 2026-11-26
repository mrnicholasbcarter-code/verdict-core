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
  `test_repo_map_handles_agents_md_without_section`,
  `test_agents_md_where_things_live_byte_cap_with_multibyte_text` (the cap
  keeps 4 094 to 4 096 bytes ending on a whole character; it fails if the
  extractor returns an empty string),
  `test_frontier_planner_prompt_carries_repo_map_with_agents_section` (the
  section and the ordered file listing are in the prompt sent to the planner).

## 2. plan_context event and receipt

- [x] 2.1 `FrontierPlanner.plan()` emits `plan_context` with `repo_map_sha256`
  after computing the map, when `events` is not None. `plan_context` is added
  to `EVENT_TYPES` in `contracts.py`. Verify:
  `test_frontier_planner_emits_plan_context_with_repo_map_sha256` (the digest
  equals the SHA-256 of the map inside the prompt the executor received),
  `test_frontier_planner_no_plan_context_without_events`,
  `test_frontier_planner_repair_call_resends_same_map_and_emits_no_second_plan_context`
  (one `plan_context` per `plan()` call; the repair prompt carries the same map).

- [x] 2.2 `receipt.py` includes `plan_context` events in `planner_attempts`.
  Verify:
  `tests/test_orch_receipt.py::test_planner_attempts_keep_plan_context_with_repo_map_digest`.
  The 60 receipt tests that existed when this task was first ticked had no
  assertion on `plan_context`: removing it from the filter left all 60 green.

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

## 6. Post-merge review corrections (#1318)

- [x] 6.1 The tests behind tasks 1.2, 2.1 and 2.2 can fail. Each new or
  strengthened test was run against a mutated `verdict/orchestration/planner.py`
  or `receipt.py` and failed, then passed with the code restored. Verify:
  `uv run --frozen python -m pytest -q tests/test_orch_planner.py tests/test_orch_receipt.py`.
- [x] 6.2 `design.md` and the spec delta state what the code does: the byte cap
  is a size bound and not injection protection, `Path.read_text()` follows an
  `AGENTS.md` symlink out of the repository, and one `plan_context` is emitted
  per `plan()` call (the validation-repair call carries the same map and emits
  none). Verify: `openspec validate planner-repo-map --strict`.
