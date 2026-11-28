# Tasks — a node's commit keeps the git-ignored files the node owns

## 1. Staging

- [x] 1.1 `Git.changed_files` force-stages ignored regular files under the owned paths; byproducts are excluded in the listing and again per path; a listing of 4,000 characters or more raises. Verify: `tests/test_orch_ignored_owned_files.py` (owned file committed and shown by `git show`, owned directory, unowned ignored file, byproduct directories and loose byproduct files past the guard, explicitly owned byproduct, oversized listing, two-node runtime test).
- [x] 1.2 Symlinks and credential-shaped files are not staged. Verify: the same test file.

## 2. Refusal and record

- [x] 2.1 `OwnedIgnoredReport` per worktree: force-staged paths, refused paths with a reason. Verify: `tests/test_orch_owned_ignored_report.py` (clean file, credential-shaped file, symlink, stray byproduct, no owned files, two worktrees of one `Git`).
- [x] 2.2 A refused path fails an implement node's attempt with `owned_file_not_committable` on the `ownership` barrier event and in the worker's feedback; the VALIDATED event lists `force_staged`. Verify: the runtime tests in the same file (always-refused run is not COMPLETE and names the reason; a clean retry ends COMPLETE with the last `ownership` barrier ok; a node without ignored files has no `force_staged` field).

## 3. Live checks

- [x] (post-merge) 3.1 On installed main, an orchestrate run whose first node adds a `*.jsonl` fixture reaches its second node with the fixture present. Verify: run `20261127T020413Z` on installed main `2b1fca69`, 2026-11-27 02:04 to 02:11 UTC: `fixture_node` VALIDATED with `force_staged: ["tests/fixtures/ac4_live/sample.jsonl"]`, `git show cdd7110b:tests/fixtures/ac4_live/sample.jsonl` prints both lines, `reader_node` verification passed on attempt 1, integration passed. The run then ended BLOCKED on its in-run review (GitHub issue #1381); that is outside this task.

## 4. Negation line (GitHub issue #1381)

- [ ] 4.1 After validation and before the commit, add `!<path>` to the root `.gitignore` for each force-staged file it un-ignores; skip a line that exists, a file still ignored after it, and a path with `.gitignore` special characters; list the paths in `unignored` on the VALIDATED event. Verify: unit tests with real git (pattern match, ignored directory, existing line, special characters) and a runtime test (commit holds the line, `git check-ignore` is silent, event fields).
- [ ] 4.2 Set `.gitignore merge=union` in the repository's local `info/attributes` when the run prepares a worktree. Verify: a runtime test with two independent nodes that each commit an ignored fixture ends COMPLETE with both lines in `.gitignore`.
- [ ] 4.3 A worker's own change to an unowned `.gitignore` still fails with `ownership_violation`. Verify: a runtime test.
- [ ] (post-merge) 4.4 On installed main, the live check of task 3.1 is repeated and ends COMPLETE; the in-run review has no finding about a missing fixture. Verify: the run's receipt and review record.

