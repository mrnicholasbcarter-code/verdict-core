# Design — what a retry of a node starts from

## Context

A DAG node gets a fresh git worktree for every attempt. BOD-308 added salvage: the owned files of an attempt that was interrupted (timeout, transport failure) are saved and restored into the next attempt. This change first stopped that restore after a rejected attempt, because a worker that emits diffs against the base revision then failed with "SEARCH text not found". The amendment of 2026-11-27 (GitHub issue #1372, defect 2) adds the case of a worker that edits files in place.

## Goals / Non-Goals

Goals:

- A retry after `diff_rejected` or `ownership_violation`, and after a failed verification of a worker that emits diffs, starts from the clean base.
- A retry after an interrupted attempt continues from the saved owned files.
- A retry after a failed verification of a worker that edits in place (terminal harness `opencode`) continues from that attempt's owned files and is told so.
- The `restore` event always says which path was taken.

Non-Goals:

- Changing the salvage rules (owned files only, size limits, credential check, base-aware comparison).
- Changing which failures count toward capability escalation.
- Re-running verification in a clean checkout of the node's commit.

## Decisions

1. **Rejected work is not restored.** `_record_failure()` marks an attempt rejected when it was validated and judged wrong, or when its category is `diff_rejected`. `_fail()` then clears the node's salvage directory, and `_attempt()` emits `restore` with `reason: "clean_base"`.
2. **Exception for in-place workers (operator decision, 2026-11-26).** A `verification_failed` attempt whose terminal harness is `opencode` is not marked rejected, so the existing interrupted path saves and restores its owned files. The terminal built after a failed validation carries the worker's harness so that `_record_failure()` can tell. The `restore` event and history entry say `reason: "verification_failed"`.
3. **The worker is told.** When files were restored after a failed verification, the retry text adds one line naming them, after the verification feedback.
4. **Scope of the exception.** `ownership_violation` keeps the clean base (the stray file must go), `diff_rejected` keeps it (the diff did not apply), and every other harness keeps it (the original failure came from there).

## Risks / Trade-offs

- A badly broken tree can hold back the next attempt. The attempt budget bounds this, and the worker is told it may change the restored files.
- The exception is keyed on a harness name. A second in-place harness will need the same treatment; a capability flag on the harness would be the general form.
- The research pack is deleted when files are restored, so retrieval runs once more for that attempt.

## Security / trust implications

Restored files pass the same salvage rules as after an interruption: only the node's owned files, with per-file and total size limits, and a file whose added content looks like a credential is skipped. No file content is written to an event; events carry paths only.

## Routing / context / memory implications

Route selection, cooldowns and escalation counting are unchanged: a failed verification still counts as a judged failure. The retry prompt gains one line. Nothing is written to memory.

## Migration / rollback

No stored state changes. Reverting the commit restores the previous behaviour. Runs in flight are unaffected because the rule is applied when a failure is recorded.

## ADR impact

None. The salvage and clean-base rules are recorded in this change, not in an ADR.
