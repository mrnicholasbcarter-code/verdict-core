# Design: Partial-work salvage and same-node restoration

## Context

`DagRuntime._attempt` creates one git worktree per attempt
(`<node_id>-a<N>`), dispatches a worker, then either validates and commits or
fails through `DagRuntime._fail`, which removes the worktree. Retry re-enters
`_attempt` with `run.attempt` incremented and a fresh worktree from the node
base. The only state carried between attempts today is textual
(`failure_feedback`, shrinking `context_budget`) plus the append-only
events/history evidence. The salvage slice inserts a bounded capture step before
worktree removal and a restore step after the next attempt's worktree is
created, both inside the existing runtime, with no new decision authority.

## Goals / Non-Goals

Goals: preserve owned-file partial work from a failed attempt; restore it into
the SAME node's next attempt; emit bounded content-free handoff evidence into
the existing events/history; reject unowned paths, credential-bearing content
and injected faults; stay no-change safe; keep acceptance, verification and
admission unchanged.

Non-Goals: cross-node or cross-run salvage; salvaging validated or integrated
work (that already persists as commits); changing recovery budgets, route
selection or failure classification; persisting salvage snapshots beyond the
run directory; content-bearing receipt data; live-run proof of savings.

## Decisions

1. The production caller is `DagRuntime` itself. `_fail` (and the
   verification-failure path that reaches it) captures salvage from the failed
   worktree before `GitOps.remove_worktree`; the next `_attempt` for the same
   node restores after `GitOps.add_worktree`. A standalone helper without this
   caller does not satisfy the slice.
2. Salvage capture diffs the attempt worktree against the attempt base and
   copies only paths inside the node's ownership contract into a per-node
   staging area under the run directory. Unowned paths are skipped and named in
   the handoff evidence, never copied.
3. Credential screening runs on each candidate file's full content before it is
   staged. A file that trips the screen is rejected whole; neither the content
   nor a partial excerpt is staged or emitted. Rejection is recorded by path
   and reason only.
4. Attempts whose terminal is fault-injected (`executor_kind ==
   "fault-injected"`) are excluded from salvage: injected chaos must not seed
   later attempts with synthetic partial work.
5. An attempt with no owned-file changes stages nothing and the retry proceeds
   exactly as before this change (no-change safe). Restore into a worktree is
   skipped when the staging area is empty or belongs to a different attempt
   lineage than the node being retried.
6. Handoff evidence is content-free and bounded: a `salvage` RunEvent and a
   history record carry relative paths, per-file byte counts, SHA-256 digests,
   and skipped path names, truncated to a fixed path count. File content never
   enters `events.jsonl`, history or the receipt.

## Risks / Trade-offs

Restored partial work can be wrong work: a retry may inherit a bad half-edit.
The existing verification command and ownership checks still gate validation,
so wrong salvage fails the same way wrong worker output fails. Staging copies
live in the run directory and add disk usage proportional to owned-file churn;
they are removed with the run directory. The credential screen is heuristic,
not proof of secret-freeness; it bounds what restoration can reintroduce but
does not certify the repository. Digest evidence identifies content without
revealing it, at the cost of not being human-readable in the receipt.

## Security / trust implications

Salvaged bytes are worker output and stay untrusted: they re-enter only a
sandboxed node worktree that must still pass ownership checks, the node
verification command and repo gates before any commit. Credential-bearing files
are rejected whole before staging. Events and receipts stay content-free, so
the digest-bound log never embeds partial file content or secrets. Salvage
never widens ownership: paths outside the node contract are rejected at
capture, so restoration cannot smuggle unowned edits into a retry.

## Routing / context / memory implications

None to route membership, admission, ranking or health evidence: salvage runs
strictly after failure classification and before the unchanged reselection
logic. `failure_feedback`, context budgets and repack behavior are untouched.
No durable memory is opened or written; staging lives in the run directory
only.

## Migration / rollback

No schema, dependency or durable-state migration. The `salvage` event type is
additive to the append-only log; existing receipts and readers are unaffected.
Rollback is removing the two runtime call sites and
`verdict/orchestration/salvage.py`; retries then revert to base-only worktrees
with textual feedback, and historical events remain valid.

## ADR impact

Implements ADR-036's bounded same-node reassignment loop without new authority:
salvage is mechanical state transfer inside the runtime, not a decision signal.
ADR 0001 invariants (admission before ranking, bounded attempts, content-free
digest-bound receipts) are preserved. No new ADR is required for this slice.

## Test strategy

`tests/test_orch_salvage.py` drives the actual `DagRuntime` with a scripted
executor: a first attempt writes owned and unowned partial work plus a
credential-bearing file and then fails verification; the test asserts the
retry's worktree contains exactly the owned, screened files, that the `salvage`
event and history record are bounded and content-free, and that a no-change
failure and an injected-fault failure salvage nothing. Helper-level tests cover
screening and ownership filtering edge cases.
