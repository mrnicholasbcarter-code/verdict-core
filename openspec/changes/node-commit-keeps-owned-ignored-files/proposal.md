# A node's commit keeps the git-ignored files the node owns

## Why

A node is VALIDATED on the tree in its worktree, but the next node builds on its commit. The commit was staged with `git add -A`, which skips git-ignored paths, so an owned file matching a `.gitignore` pattern (a `*.jsonl` test fixture here) was verified and then left out. In run `20261126T180407Z` the next node failed on `FileNotFoundError` for exactly that reason and the run ended BLOCKED (GitHub issue #1372, defect 1).

This change records the behaviour that pull requests #1377 and #1379 built without spec text.

## What Changes

**`verdict/orchestration/runtime.py`**

- `Git.changed_files` also force-stages untracked, git-ignored regular files that lie under the node's owned paths. It lists them with one `git ls-files` call limited to the owned paths, with every byproduct pattern (`BYPRODUCT_EXCLUDES`) excluded in the call itself, and refuses a listing of 4,000 characters or more, because the command runner keeps only the last 4,000 characters of output.
- In this step it never stages a byproduct, a symlink, a path that is not a regular file, a file it cannot read, or a file whose content looks like a credential. (An owned path under a byproduct directory is still force-added by the earlier, unchanged step.) For each call it records, per worktree, which paths it force-staged and which it refused and why (`OwnedIgnoredReport`).
- `_validate` fails an implement node's attempt when a path was refused: the `ownership` barrier event is not ok and carries `refused_paths`, and the worker's feedback starts with `verification_failed: owned_file_not_committable:`.
- The VALIDATED `node_state` event carries `force_staged` when any path was force-staged.

**Amendment (GitHub issue #1381, not built yet)**

- Before committing a VALIDATED node, the runtime adds a `!path` line to the root `.gitignore` for each force-staged file that such a line un-ignores, and lists those paths in `unignored` on the VALIDATED event. `.gitignore` is merged with a union merge while the run integrates.

## Capabilities

### New Capabilities

- `orchestration-node-commit`: what a validated node's commit contains when the node owns files that git ignores, and what happens when such a file cannot be committed.

## Impact

`verdict/orchestration/runtime.py`; tests `tests/test_orch_ignored_owned_files.py` and `tests/test_orch_owned_ignored_report.py`. No stored state, schema or CLI change. The `node_state` and `barrier` events gain optional fields.

## Evidence boundary

- **KNOWN:**
  - Unit and runtime tests in the two test files above pin every scenario of the spec delta.
  - Live, 2026-11-27, installed main `2b1fca69`, run `20261127T020413Z`: a node that added `tests/fixtures/ac4_live/sample.jsonl` was VALIDATED with `force_staged` naming that path, its commit holds the file, and the dependent node passed its verification on the first attempt with the file present.
  - The same run ended BLOCKED because the in-run review did not see the force-staged file and reported it missing (GitHub issue #1381).
  - Negation, 2026-11-27 04:50 UTC, same reviewer (`ocr` v1.12.9 on `cc/claude-opus-5-5`) and rule file as the blocked run: the range ending at the live check's integration commit `1cffd0fd` gives the two false findings again; the same range plus one `.gitignore` line `!tests/fixtures/ac4_live/sample.jsonl` (a local commit, never pushed) gives 0 findings, with `.gitignore` among the selected items.
  - In a scratch repository: a negation does not re-include a file under an ignored directory; two branches that each append a line to `.gitignore` conflict on a plain merge and merge cleanly with `merge=union` in the local `info/attributes`, which a linked worktree reads.
- **INFERRED:** an explicit refusal is more useful to a worker than a silent skip. No run has shown a refusal live yet.
- **NOT AVAILABLE:** what a run does when the 4,000-character guard fires (the unit test shows the staging call raises; no runtime-level test covers it); a run that adds an ignored file and ends COMPLETE (the amendment for #1381 is specified here and not built); any report of ignored files that the node does not own (not built, see Non-Goals in the design).

## Acceptance criteria

- A node that creates an owned file matching a `.gitignore` pattern: its commit contains the file, the dependent node's worktree has it, and the VALIDATED event lists the path in `force_staged`.
- Byproducts under an owned directory are never staged and never make the listing guard fire.
- An owned ignored file that is a symlink, is not a regular file, cannot be read or has credential-shaped content is never staged, and the attempt fails with `owned_file_not_committable`; no file content appears in an event or in the worker's feedback.
- (post-merge) On installed main, an orchestrate run whose first node adds a `*.jsonl` fixture reaches its second node with the fixture present.
- A node that commits an ignored file matched by a pattern also commits its `.gitignore` negation line; two such independent nodes integrate without a conflict; a worker's own edit of an unowned `.gitignore` is still an ownership violation.
- (post-merge) On installed main, an orchestrate run whose first node adds a `*.jsonl` fixture ends COMPLETE with an in-run review that has no finding about a missing fixture.
