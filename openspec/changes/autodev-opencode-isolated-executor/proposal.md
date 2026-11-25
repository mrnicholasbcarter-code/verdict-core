# OC2a: OpenCode autodev rung, isolated executor behind an explicit flag

## Why

`verdict autodev` (#1301) repairs code in the user's repository, and its model tier (`PatchExecutor`)
cannot run the unit's tests first. OpenCode runs an edit, test and fix loop, so on free routes each unit
gets more chances to pass at no cost (the mission; ADR-053 Decision 3). PR #1299's review found three
defects: OpenCode edited the user's repository directly, leaving failed attempts on disk (1); its context
file was attributed to the unit (2); retries left OpenCode (3). This change closes 1 and 2 (the executor no
longer writes a context file since #1336, so 2 holds by construction and by test); 3 is OC2b (#1314).

## What Changes

- New `verdict/autodev_opencode.py`, `OpenCodeUnitExecutor`: per attempt, an unshared git clone seeded
  from the user's tree, OpenCode run there, the change computed from file contents (no git in the
  clone), every changed path checked against `owned_files`, only validated changes copied back.
- `verdict/autodev_run.py`: the executor becomes a protocol; `_run_unit_once` restores on every exit after
  a transfer but a verified success (false verify, exception, `KeyboardInterrupt`); retries are refused.
- Flag `--worker-harness opencode` and a readiness check before any probe or planner call: one message
  and exit 1 when opencode, serve or the credentials the executor signs with are unavailable, no fallback.
- **Narrowed guarantee.** With the flag, a unit whose owned file has uncommitted changes, is untracked,
  ignored or hidden from `git status` (assume-unchanged, skip-worktree, a `filter` or `ident` attribute)
  is refused: a crash cannot run the in-memory undo, so git must hold the user's bytes. The OpenCode
  executor then writes nothing; an edit the mechanical tier already made stays, as on the default path. A
  crash during the executor's own writes leaves each owned file old-or-new and `git checkout` recovers it;
  the verification command's writes are outside that. The alternative is an on-disk journal (design D5).

## Capabilities

### New Capabilities

- `autodev-opencode-executor`: an opt-in executor that runs OpenCode in an unshared clone and copies back
  only validated owned-file changes.

## Impact

Without the flag nothing changes (`PatchExecutor` and the retry ladder are untouched). With it the
mechanical tier still runs first in the user's tree. Not here: OpenCode retries, lineage, default
selection, credential delivery (#1313), `packet-execute`, a live run. #1299 (OC1) has merged;
design.md "Drift after #1299" records what changed since this was written.

## Evidence boundary

- **KNOWN [main]:** `_touched_since` and `_verify` run outside any handler, `_run` catches only
  `FileNotFoundError` and `TimeoutExpired`, `_verify` runs the unit's command in the user's tree, and the
  mechanical tier can leave a partial `ruff --fix` edit. **KNOWN [exp]** (git 2.56.1): see design.md.
  **KNOWN [main `44be580c`]:** `OpenCodeExecutor.run` never raises, writes nothing into `cwd` and needs a
  run id and a node id (design.md, "Drift after #1299").
- **INFERRED:** serve can refuse a route autodev admitted (#1306); the unit fails, nothing is written.
  **NOT AVAILABLE:** a live run through serve (OC2b); the worker's test loop without `.venv`.

## Acceptance criteria

- Out-of-bounds edits (an unowned file whose bytes change, whether or not the worker also staged or
  committed it; a new unowned file; a symlink swap) leave the repository byte-identical to before the
  attempt (git-only state that leaves the bytes as seeded is not seen, and reaches nothing); an owned
  repair is accepted and nothing but the owned change reaches the repository.
- Every exit after a transfer except a verified success restores the owned files, including a runner
  `PermissionError` and a `KeyboardInterrupt` right after any mutation, without hiding the original error
  or overwriting a later user edit. A failed write leaves no temporary file or directory.
- After `SIGKILL` during a transfer each owned file holds its old or new bytes and git restores it
  (conversions as git writes them); an owned file git cannot restore is refused; staged renames seed.
- With opencode, serve or its credentials unavailable autodev stops with one message before any probe;
  retries are refused.
