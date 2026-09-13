---
name: verdict-dispatch
description: Use when a hydrated Verdict issue is ready for an explicitly selected worker or a stopped attempt needs safe redispatch.
---

# Verdict dispatch

Read `docs/guides/prime-workflow.md` and the current packet. Recheck Linear revision,
dependency readiness, source/worktree ownership and live target immediately before dispatch.

1. Inspect `git worktree list --porcelain`; reuse the recorded issue worktree/branch if compatible.
   Create it from the packet's fetched base SHA only if absent. A branch checked out elsewhere
   must be reused there or blocked, never forcibly checked out twice. Preserve dirty changes.
   Then acquire the durable single-writer lease with `acquire_lease` from
   `scripts/prime_workflow.py` (issue, `dispatch_id` = packet `attempt_id`, `worker_id`,
   worktree, branch, `stale_after_seconds`, supervisor identity). A live unstale lease blocks:
   stop/supersede that writer first, never race it. PID existence alone is not ownership, and
   `generation` is the fence token that invalidates every superseded writer.
2. Discover actual models with `prime-agent model list` or `await rlm.find_models(query)`.
   Use existing Verdict route/explain/chooser to validate capability, privacy, freshness and
   budget, then persist the exact provider/model, routing evidence and reason. An automatic
   alias (`auto/*`, `best`, default) is not an exact execution target. Observe any upstream
   remapping/failover; reject unapproved target substitution rather than relabeling the receipt.
3. Launch a **synchronous owned** worker with explicit `--cwd`, `--provider`, `--model`,
   `-p --mode json` and the packet path. Use the guide's process supervisor for unattended work.
   Give it lean Spec Kit implement, file boundaries, evidence paths, and the receipt schema.
   Under the supervised loop, pass the unique supervisor session directory to child clients; do not detach RLM write workers: the
   supervisor must own the client process group and confirm daemon session cleanup. Read-only RLM research must finish before return.
4. Persist attempt ID, process ownership, start/deadline, target and packet digest. Advance to
   IMPLEMENTING only on observed admission, and checkpoint it with `write_checkpoint` so a cold
   restart resumes the same attempt. Heartbeat the lease with `heartbeat_lease(..., progress=False)`
   for liveness and `progress=True` only for a substantive result: new commit or diff, executed
   test/proof, registered artifact, receipt step transition, documented blocker or escalation
   request. Log volume and repeated reads are not progress. Poll boundedly; an admission handle is not a result.
   `rlm.run` has **no cwd parameter** and returns admission only. `rlm.list_subagents()` returning
   `[]` does not demonstrate successful implementation.
5. Require terminal `receipt.json`: version, issue, attempt, worktree/base/head SHA, provider,
   model, status, changed files, commands with exit/evidence, proof path, blockers and summary,
   plus the progress contract (`dispatch_id`, `issue_id`, `worker_id`, `started_at`,
   `last_progress_at`, `current_step`, `objective`, `files_changed`, `git_head`, `commands_run`,
   `tests_run`, `proof_collected`, `next_action`, `needs_rehydration`, `needs_escalation`, and
`lease_generation` matching the packet's current fence token.
   Run `validate_receipt(receipt, packet)` from `scripts/prime_workflow.py`. Reconcile against
   actual git diff, subprocess exit and evidence. A matching COMPLETE receipt enters
   LOCAL_VALIDATION, not PROOF_COMPLETE; parent validation is still mandatory.
6. On timeout/no progress, stop and reap the owned process group, supersede the fenced lease,
   and only then retry. Capture
   the failure and retained dirty diff, increment attempt, call hydrate-context again, then
   choose an eligible target anew. A worker awaiting input is a surfaced blocker, not idle
   success. Never exceed the guide's recovery budgets or steal an external writer.
