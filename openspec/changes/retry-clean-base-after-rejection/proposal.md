# retry-clean-base-after-rejection: start from the clean base after a REJECTED attempt

## Why

BOD-308 added partial-work salvage for interrupted node attempts (SIGKILL, timeout, transport
failure). After an interruption, the half-finished owned files are saved to a salvage directory
and restored into the next attempt's worktree so the worker can continue where it left off.

The salvage restore was also applied to REJECTED attempts — cases where an independent checker
judged the work as wrong (verification_failed, diff_rejected). This was a bug: the next attempt
still received the rejected implementation in its worktree, causing "SEARCH text not found"
failures when the worker's prompt described the base revision's files but the worktree contained
the wrong prior implementation.

## Amendment (2026-11-27, GitHub issue #1372 defect 2)

The clean-base rule stays for workers that emit diffs, which is where "SEARCH text not found"
came from. For a worker that edits files in place (terminal harness `opencode`) a
`verification_failed` attempt is no longer thrown away: its owned files are salvaged and restored,
the restore event says `reason="verification_failed"`, and the retry text tells the worker its
files are already there. Reason: in two self-build runs (`20261126T180407Z`, `20261127T000415Z`) a node
lost a nearly passing attempt to a clean base and had to redo it inside the same attempt limit. Operator
decision recorded on the issue (2026-11-26). `ownership_violation` and `diff_rejected` keep the
clean base.

## What Changes

**`verdict/orchestration/runtime.py`**

- New constant `_JUDGED_WORK_CATEGORIES = frozenset({"diff_rejected"})`: categories that arrive
  as TERMINAL_FAILURE but mean "work was judged wrong" (not an infrastructure interruption).
- New field `NodeRun.last_rejected: bool` tracks whether the most recent failure was judged wrong.
- `_record_failure()`: sets `run.last_rejected = validated or category in _JUDGED_WORK_CATEGORIES`
  and records `"rejected": run.last_rejected` in the history entry.
- `failure_feedback` block: adds a `diff_rejected` case alongside the existing
  `verification_failed` case so the next attempt receives the error as advisory context.
- `_fail()` salvage block: when `run.last_rejected`, clears any existing salvage with
  `clear_salvage_dir()` instead of saving new partial work. Only interrupted failures save.
- `_attempt()` restore block: when `run.last_rejected` (previous attempt was judged wrong),
  emits a restore event with `reason="clean_base"` and no files, then resets the flag.
  For interrupted failures, emits the restore event with `reason="interrupted"` as before,
  and additionally deletes the cached research pack
  (`run_dir/context_artifacts/{node_id}.json`) so the hydrator re-reads the restored state.

**`verdict/orchestration/recovery.py`**

- Adds explicit `diff_rejected` classification before the `unknown` fallthrough:
  `category="diff_rejected"`, `action="CORRECT_IMPLEMENTATION"`, `cooldown_seconds=0`,
  `scope="none"`. The route is not at fault; the fix is in the implementation.

**`verdict/orchestration/salvage.py`**

- Renames `_clear_existing_salvage_dir` to `clear_salvage_dir` (public) with a docstring.
  Keeps the old private name as an alias for internal callers.

**`tests/test_orch_salvage.py`**

- Three new tests in `TestDagRuntimeSalvageRestore`:
  - `test_rejected_attempt_starts_from_clean_base`: verification-rejected → no salvage, restore
    event has `reason="clean_base"`, second attempt sees empty worktree.
  - `test_diff_rejected_category_starts_from_clean_base`: diff_rejected category → same clean
    base behaviour as a verified rejection.
  - `test_interrupted_restore_emits_reason_and_clears_research_pack`: interrupted failure →
    salvage saved, restore event has `reason="interrupted"`, stale research pack is deleted.

## Impact

`verdict/orchestration/runtime.py`, `verdict/orchestration/recovery.py`,
`verdict/orchestration/salvage.py`, `tests/test_orch_salvage.py`.

REJECTED retry loops that produced "SEARCH text not found" failures are fixed. Interrupted
salvage/restore is unchanged in behaviour; the restore event gains a `reason` field.
