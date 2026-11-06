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
- [ ] 2.1 New task-check tests fail on unchanged `scripts/check_openspec_tasks.py`.
- [ ] 2.2 New definition-of-done tests run on unchanged `scripts/check_dod.py`.

## 3. Implementation
- [ ] 3.1 `scripts/check_openspec_tasks.py` splits open tasks into post-merge
  and blocking, fails only on blocking ones, and lists both.
- [ ] 3.2 `scripts/drill_gates.py` adds the `openspec-near-miss-marker` drill.

## 4. Docs
- [ ] 4.1 AGENTS.md "Definition of done", CONTRIBUTING.md and the docstrings of
  `scripts/check_openspec_tasks.py` and `scripts/check_dod.py` describe the marker.

## 5. Review
- [ ] 5.1 Cross-family review of the diff; every finding is fixed or declined
  with a reason.

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
