---
name: verdict-resume
description: Use when starting or resuming Verdict autonomous development, recovering interrupted or stalled workers, or continuing after context compaction.
---

# Verdict resume

Read `docs/guides/prime-workflow.md` from the repository root first. Its contracts,
authority rules, budgets and failure handling apply to all five workflow skills.

1. Identify the repository with git, read project instructions, and locate the common-git
   `verdict-prime` directory. Read checkpoint, run, outcome, packet, receipts, leases and
   supervisor recovery reason; `python3 scripts/prime_workflow.py status --state-dir "$STATE"`
   returns all of it in one call, and `next_action_from_checkpoint` is the cold-resume
   projection. These are hints, not truth. Never require prior conversation.
2. Check live tools once with bounded timeouts: Linear planning access, git, GitHub,
   Prime model registry, exact OmniRoute target, required context providers. Use existing
   capabilities rather than creating skills for them. Optional MCP outages become explicit
   omissions. Missing planning/code authority or required routing evidence blocks execution.
3. Fence stale writers before assigning anyone: `supersede_stale_leases` (CLI `lease-reap`)
   marks every lease whose `last_progress_at` exceeded its staleness bound as SUPERSEDED, so a
   replacement writer acquires a higher `generation` and the old writer cannot heartbeat, release
   or open a PR. Reconcile Linear issues and dependency relations (all pages), git
   branches/worktrees, and GitHub PR/check/merge facts. Inspect existing claimed work before selecting new work.
   Missing/idle child is not completion. Preserve dirty work; inspect its receipt and diff.
   If GitHub says merged but main was not tested, resume at MERGED and run main verification.
   Never reopen already merged work because a checkpoint or Linear status lags.
4. For active work, identify its exact last evidence-backed state and resume the next gate.
   For new work, filter dependency-READY issues within the configured Linear project/team.
   Unknown dependencies block. Sort priority 1,2,3,4,0, then issue identifier; skip blocked
   issues and record why. Never guess project scope from unrelated workspace tasks.
5. Apply lean Spec Kit **specify -> plan -> tasks -> implement**, reusing existing artifacts
   and preserving installed hooks. Follow the guide's fresh-worktree adaptation when Spec Kit
   shell scripts are absent, using the installed `spec-driven-development` skill and templates.
   Use existing `documentation-and-adrs` for architectural
   decisions: inspect current ADRs, update references or supersede as appropriate, do not
   create duplicate decisions. Planning belongs before hydration; implementation after dispatch.
6. Invoke **hydrate-context**, then **verdict-dispatch**, **verdict-proof**, and
   **verdict-finish**, loading their full bodies. Continue to the next READY issue after DONE
   until the run budget is reached or no actionable issue remains. Do not stop after planning,
   coordination, admission or a worker's success claim.
7. After each phase write the checkpoint through `write_checkpoint`, which validates the full
   field list, refuses to regress an issue to an earlier lifecycle state, and carries
   `completed_issues` across issue switches. Before compaction persist it, inspect `await compact.status()`, and compact
   at the guide's context budget. Reload the checkpoint and revalidate current authorities
   afterward. Summary prose never substitutes for the packet or proof.

## Stall recovery

Use the external supervisor for unattended execution. Every 60 seconds inspect worker
status and substantive progress. Heartbeats, repeated reads, polling, promises and evidence-file
churn do not reset the progress deadline: progress is a substantive source/state change or a
receipt transition (`dispatch_id`/`status`/`current_step`/`last_progress_at`). At 10 minutes without progress, diagnose the last action,
tool timeout, worker roster, diff and evidence. Stop/reap only the owned worker before
redispatch; never create a competing writer. Rehydrate in a fresh context with the failed
action and a narrower next step. Two recoveries maximum per supervisor invocation;
persist blocked issues and try independent READY work. If authority/transport is down,
checkpoint and report one actionable blocker instead of retrying forever.

Finish the run with a matching `outcome.json` (DONE, IDLE or BLOCKED plus reason).
DONE requires verified issue completion, not merely exhausting the attempt budget.
