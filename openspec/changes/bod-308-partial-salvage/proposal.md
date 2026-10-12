# Proposal: BOD-308 first slice: safe partial-work salvage on the production retry path

## Why

**KNOWN**: At the current base, `verdict/orchestration/runtime.py` discards a failed
attempt's worktree in `DagRuntime._fail` (`GitOps.remove_worktree`) before the same
node is re-dispatched. Any owned-file partial work from the failed attempt is lost,
and the next attempt starts from the node base plus only textual
`failure_feedback`. A worker that fixed half of the objective before a
verification failure repeats that work on every retry.

**KNOWN**: The retry path already carries bounded, append-only evidence per
attempt: `RunEvent` entries in `events.jsonl` and `NodeRun.history` records. The
receipt is digest-bound to that log.

## What Changes

- Add `verdict/orchestration/salvage.py`: capture owned-file partial work from a
  failed attempt's worktree before removal, and restore it into the SAME node's
  next attempt worktree.
- Wire the real production caller: `DagRuntime` in
  `verdict/orchestration/runtime.py` invokes salvage on the failure path before
  the failed worktree is removed, and restoration on the next attempt of the
  same node. A helper without this caller is not completion.
- Record bounded, content-free handoff evidence in the existing event log and
  node history: paths, counts and digests only, never file content.
- Enforce rejection boundaries: unowned paths are never salvaged,
  credential-bearing partial content is rejected, injected-fault attempts are
  excluded, and an attempt with no owned-file changes salvages nothing
  (no-change safe).
- Leave node acceptance, verification, admission and route selection unchanged.

## Capabilities

### New Capabilities

- `partial-salvage`: Safe preservation and same-node restoration of owned-file
  partial work across worker retry attempts, called by the production DAG
  runtime.

## Impact

**KNOWN**: Owned files are `verdict/orchestration/runtime.py`, new
`verdict/orchestration/salvage.py`, `tests/test_orch_salvage.py`, this OpenSpec
change, `docs/guides/orchestration-golden-path.md` and `tests/test_bod308_docs.py`.
No pushes, no merges to main, no dependency installs, no provider settings, no
credential or global registry edits.

**INFERRED**: Restoring salvaged owned-file work should reduce repeated effort on
retries of the same node without widening what a retry may touch, because the
salvage set is bounded by the node's ownership contract.

**NOT AVAILABLE**: Live orchestration evidence that salvage shortens real retry
work is not available in this slice. Focused fixture tests exercise the actual
`DagRuntime` failure-then-retry restoration; they do not measure live savings.

## Acceptance Criteria

- [ ] `DagRuntime` preserves owned-file partial work before a failed attempt's
  worktree is removed and restores it into the same node's next attempt.
- [ ] Handoff evidence in events and history is bounded and content-free.
- [ ] Unowned paths, credential-bearing content and injected-fault attempts are
  rejected; a no-change attempt salvages nothing and retries unchanged.
- [ ] Existing node acceptance, verification and admission behavior is unchanged.
- [ ] Focused tests exercise the actual `DagRuntime` failure-to-retry
  restoration, not only the helper.
