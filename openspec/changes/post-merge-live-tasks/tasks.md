# Tasks

Owned files: `scripts/check_openspec_tasks.py`, `scripts/check_dod.py`
(docstring only), `scripts/drill_gates.py`, `tests/test_check_openspec_tasks.py`,
`tests/test_check_dod.py`, `AGENTS.md`, `CONTRIBUTING.md`; this change.

## 1. Spec
- [x] 1.1 Proposal, design and the `repository-tooling` spec delta are written,
  and the pinned OpenSpec CLI validates this change with `--strict`. Result on
  `2ab6ed75`: openspec 1.13.2 printed `Change 'post-merge-live-tasks' is valid`,
  exit 0.

## 2. Tests first
- [x] 2.1 New task-check tests fail on unchanged `scripts/check_openspec_tasks.py`.
  Result on `2ab6ed75` with only the tests changed: 4 failed, 61 passed across
  `tests/test_check_openspec_tasks.py` and `tests/test_check_dod.py`. The three
  `test_open_post_merge_task_passes_and_is_listed` cases failed with
  `assert 1 == 0`, and `test_plain_open_task_beside_a_post_merge_task_fails`
  printed `openspec change new: 2 open task(s)`. The 16 near-miss cases passed,
  as they must: the old check fails every open task.
- [x] 2.2 New definition-of-done tests run on unchanged `scripts/check_dod.py`.
  All three passed, because `_openspec()` already fails on any `- [ ]` line and
  on an active change. A temporary edit of its pattern to skip post-merge lines
  turned `test_open_post_merge_task_is_not_done` and
  `test_archive_with_open_post_merge_task_fails_all[True]` red. The `[False]`
  case stays green there, because an archive with no evidence file fails
  without reading `tasks.md`.

## 3. Implementation
- [x] 3.1 `scripts/check_openspec_tasks.py` splits open tasks into post-merge
  and blocking, fails only on blocking ones, and lists both. After: 67 passed
  across the two files and `tests/test_drill_gates.py`. A temporary lenient
  marker, `(?i).*post.?merge`, turned all 16 near-miss cases red.
- [x] 3.2 `scripts/drill_gates.py` adds the `openspec-near-miss-marker` drill.
  A local run on `62f32782` with the pinned OpenSpec CLI passed every control,
  printed `drill openspec-near-miss-marker: FAILED as intended (tasks exit 1)
  openspec change drill-near-miss-marker: 1 open task(s)`, and ended
  `gate drills: all drills failed as intended`, exit 0.

## 4. Docs
- [x] 4.1 AGENTS.md "Definition of done", CONTRIBUTING.md and the docstrings of
  `scripts/check_openspec_tasks.py` and `scripts/check_dod.py` describe the marker.
  `scripts/check_doc_links.py`: `doc links ok (460 files checked)`.

## 5. Review
- [x] 5.1 Cross-family review of the diff; every finding is fixed or declined
  with a reason. Round 1 (`cx/gpt-5.6-luna-high`, full diff of `f124a195`
  against `2ab6ed75` plus the operator decision): APPROVE, "no finding" in
  correctness, security and fail-closed, tests, and spec/tasks. No later round
  was needed.

## 6. Live checks and archive (pending; needs merged main)
None of these can run before the merge. Nothing below has run.
- [ ] (post-merge) 6.1 After this merges and PR #963's live tasks are relabelled
  `- [ ] (post-merge) ...`, run `python scripts/check_openspec_tasks.py --base
  origin/main` on that branch, updated with merged main. It must exit 0 and list
  the relabelled tasks as post-merge. Record the date, SHA, command and raw output.
- [ ] (post-merge) 6.2 A `gate-drills.yml` run on merged main passes, including
  the `openspec-near-miss-marker` drill. Record the run URL.
- [ ] (post-merge) 6.3 Build the definition-of-done evidence file and commit it
  with this change archived, only when `scripts/check_dod.py` passes on it. That
  needs 6.1 and 6.2 ticked first.
