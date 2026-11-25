# OC2a: OpenCode autodev rung, isolated executor behind an explicit flag

## Why

`verdict autodev` (#1301) repairs code in the user's repository, and its model tier (`PatchExecutor`)
cannot run the unit's tests first. OpenCode runs an edit, test and fix loop, so on free routes each unit
gets more chances to pass at no cost (the mission; ADR-053 Decision 3). PR #1299's review found three
defects: OpenCode edited the user's repository directly, so failed or out-of-bounds attempts stayed on
disk (1); its context file was attributed to the unit (2); retries left OpenCode (3). This change closes
1 and 2; defect 3 is OC2b (#1314), split by the operator on 2026-11-24.

## What Changes

- New `verdict/autodev_opencode.py`, `OpenCodeUnitExecutor`: per attempt, an unshared git clone seeded
  from the user's tree, OpenCode run there, the change computed from file contents (no git in the
  clone), every changed path checked against `owned_files`, only validated changes copied back.
- `verdict/autodev_run.py`: the executor becomes a protocol; `_run_unit_once` restores on every exit
  after a transfer except a verified success (false verify, exception, `KeyboardInterrupt`);
  `run_autodev` refuses retries for an isolated executor.
- Flag `--worker-harness opencode` and a readiness check before any probe or planner call: one message
  and exit 1 when opencode or serve is unavailable, never a fallback to `PatchExecutor`.
- **Narrowed guarantee.** With the flag, a unit whose owned file has uncommitted changes, is untracked
  or is ignored is not sent to the worker: a process crash cannot run the in-memory undo, so git must
  hold the user's bytes. A crash then leaves each owned file old-or-new and `git checkout` recovers it.
  The default path is unchanged. The alternative is an on-disk journal (design D5).

## Capabilities

### New Capabilities

- `autodev-opencode-executor`: an opt-in executor that runs OpenCode in an unshared clone and copies back
  only validated owned-file changes.

## Impact

Without the flag nothing changes (`PatchExecutor` and the retry ladder are untouched). With it
`run_autodev` gets `retry_routes=()` and the mechanical tier still runs first in the user's tree. Not
here: OpenCode retries, lineage, default selection, credential delivery (#1313), `packet-execute`, a
live run. The build waits on #1299 (OC1); the [U] facts are re-read by task 1.1.

## Evidence boundary

- **KNOWN [main]:** units run in sequence and own disjoint files; `_touched_since` and `_verify` run
  outside any handler and `_run` catches only `FileNotFoundError` and `TimeoutExpired`; the mechanical
  tier can leave a partial `ruff --fix` edit. **KNOWN [exp]** (git 2.56.1): see design.md. **KNOWN
  [U]** (OC1 `f684a7df`): `OpenCodeExecutor.run` never raises and writes `.verdict-context.md` into `cwd`.
- **INFERRED:** serve can refuse a route autodev admitted (#1306); the unit fails, nothing is written.
  **NOT AVAILABLE:** a live OpenCode run through serve (OC2b); the worker's test loop without `.venv`.

## Acceptance criteria

- Out-of-bounds edits (plain, staged, committed, new unowned file, symlink swap) leave the repository
  byte-identical; an owned repair is accepted although the context file was created.
- Every exit after a transfer except a verified success restores the owned files, including a runner
  `PermissionError` and `KeyboardInterrupt`, without hiding the original error or overwriting a later
  user edit. A failed write leaves no temporary file or directory.
- After `SIGKILL` during or after a transfer each owned file holds its old or new bytes and git restores
  it; an owned file git cannot restore is refused. A staged rename seeds correctly.
- With opencode or serve unavailable autodev stops with one message before any probe; retries are refused.
