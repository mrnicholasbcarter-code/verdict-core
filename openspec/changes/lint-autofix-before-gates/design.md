# Design

## Context

`verdict orchestrate` runs workers that write code, then verifies and gates the
output. The old flow was: worker writes → verification_command → `_run_repo_gates`
(ruff-check, ruff-format, mypy). A reactive `_repair_formatting` path tried to
fix `ruff-format` failures after the gate detected them, but: (a) it ran after
the gate already failed, so it only helped on the retry, and (b) it did not
handle `ruff-check` (lint) failures at all.

The result: an implementation that was functionally correct but had an auto-fixable
lint issue (unused import, naming convention) consumed an entire attempt and issued
a confusing correction prompt to the next worker.

## Goals / Non-Goals

**Goals:**
- Apply safe lint fixes (`ruff check --fix`, `ruff format`) to the node's owned
  changed files before both `verification_command` and `_run_repo_gates`, so both
  see the corrected code.
- Replace `_repair_formatting` entirely; remove the reactive path.
- Record the autofix outcome as a `lint_autofix` event in the event log and
  receipt.
- Fail safe: if ruff is missing, emit the event with `ruff_missing=True` and
  continue.
- Never widen the node's file scope.
- Never pass `--unsafe-fixes`.

**Non-Goals:**
- Fix mypy or other non-ruff errors automatically.
- Run autofix on INTEGRATE or REVIEW nodes.
- Commit the autofix changes in a separate commit — they are part of the attempt
  diff and committed as such by the runtime.

## Decisions

1. **Placement: before verification and gates.** Putting autofix before
   `verification_command` means the verifier also sees fixed code, not just the
   gates. This is better than the old reactive path (fix after gate fails) because
   it eliminates the spurious gate failure event and the unnecessary retry cycle.

2. **Gate-gated activation.** `_apply_lint_autofix` checks `discover_repo_gates`
   first. If ruff is not a declared gate (`ruff-check` or `ruff-format`), it
   emits a skipped event and returns. This avoids imposing a tool the repo has
   not chosen, and is consistent with how `_run_repo_gates` already derives its
   tool list from declared configuration.

3. **Safe fixes only.** Only `ruff check --fix` (no `--unsafe-fixes`) is called.
   Safe fixes are those ruff classifies as having no semantic risk. This matches
   the original intent of `_repair_formatting` (mechanical fixes only).

4. **File scope guard.** Only files that are owned by the node (`node.owns(p)`),
   exist as real files (no symlinks), and lie within the worktree root are passed
   to ruff. This prevents scope creep and symlink-traversal attacks.

5. **Replace, do not extend, `_repair_formatting`.** Keeping both paths would
   create two autofix points with conflicting sequencing. The old reactive path
   is removed entirely; `_apply_lint_autofix` covers both the check and format
   cases proactively.

6. **`lint_autofix` event.** The event carries: `files` (targets passed to ruff),
   `changed_files` (files whose digest changed), `ruff_check_exit`,
   `ruff_format_exit`, `before_digests`, `after_digests` (first 16 chars),
   `skipped`, `ruff_missing`. Receipt records `files_changed`, `ruff_missing`,
   `skipped` per attempt.

7. **`EVENT_TYPES` registration.** `contracts.py` validates event types in
   `RunEvent.__post_init__`. The new type `"lint_autofix"` must be registered or
   runtime emission raises `OrchestrationError`.

## Risks / Trade-offs

- **Autofix changes files the worker wrote.** This is intentional: trivial lint
  fixes (unused imports, name casing) are safe to apply mechanically. Semantic
  correctness is not affected. If ruff incorrectly fixes something, the
  verification command will catch it.
- **Worker does not see the autofix.** The worker wrote code with the lint issue;
  the runtime fixed it silently. This is acceptable for safe fixes. If the same
  issue recurs (ruff re-introduces the error), the gate will still catch it and
  report correction feedback.
- **Ruff version parity.** Autofix uses the same ruff binary as the gates (via
  `_resolve_gate_argv`), so the fix and the check use the same rule set.

## Security / trust implications

- Autofix is bounded to the node's owned files and to paths within the worktree
  root. Symlinks are skipped. No path-escape is possible.
- `--unsafe-fixes` is never passed. Only ruff-safe transformations are applied.
- The event log records before/after digests; the receipt is digest-bound.
  Autofix changes appear in the attempt diff and are part of the receipt's
  content hash.

## Routing / context / memory implications

- No effect on route selection, admission, or ranking.
- Advisory signals and decision signals are not affected.
- Context packs are not affected.

## Migration / rollback

- No data migration. Roll back by reverting the `_apply_lint_autofix` addition
  and the `_repair_formatting` deletion, and removing `"lint_autofix"` from
  `EVENT_TYPES`.
- Existing receipts without `lint_autofix` in node records are unaffected; the
  field is optional in the per-attempt record.

## ADR impact

- ADR-001 (control plane invariants): admission/ranking untouched. Attempts
  remain bounded (autofix is O(1), no retry triggered by autofix itself).
- No new ADR required; this is a runtime quality-of-life improvement within the
  existing architecture.
