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
2. Use the owned runtime for every worker task. Pass the exact active controller model into
   the bridge; that identity is dynamically excluded from workers. The standard worker scope
   is the hard allowlist `cc/*` and `kr/*`. Routes outside that scope, including Antigravity,
   are rejected before probing or spawning unless the operator explicitly changes the scope.
   Never use default/auto targets and never convert the controller into a worker fallback.
   The runtime intersects the complete `prime-agent model list` with live OmniRoute inventory,
   canonical Verdict availability, measured usable-capacity evidence, route policy and task
   capabilities before ranking. Discovery and connection are not availability. Zero quota,
   missing required usage evidence, stale/unhealthy evidence and excluded providers cannot be
   ranked back in. No first-N probe or candidate cutoff applies.
3. Start the controller through the native Prime bridge, then **end the turn**:

   ```python
   import runpy

   dispatch = runpy.run_path(
       "/home/nick/dev/verdict-core/.prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py"
   )
   operation = dispatch["start"](
       rlm,
       bash,
       "/home/nick/dev/verdict-core",
       worker_prompt,
       controller_model="cc/claude-fable-5-1",  # exact active controller for this session
       task={"required_capabilities": ["tools"], "coding": True},
       budget={"total_seconds": 900, "attempt_seconds": 180},
   )
   print(operation.directory, operation.handle.pid)
   ```

   Replace `controller_model` with the exact controller used to start the current Prime session.
   The bridge injects the active controller exclusion and `cc/*`/`kr/*` worker boundary into the
   durable task config and enforces the same boundary again at spawn time.

   Use the current checkout path, not another worktree. The external controller runs
   in that checkout's `.venv`; the stdlib bridge only services native
   `rlm.spawn(prompt, name=..., model=selected_selector)`, nonblocking `rlm.collect`,
   and owned deletion. Keep `operation` alive. Do not await its bridge or poll in the
   foreground. A bash completion follow-up wakes the parent even after a child fails.
   On completion inspect `operation.result()` and the saved `events.jsonl`.
   Return exactly one explicit final operation state: `SUCCESS` with validated full
   worker output/model/spawn provenance, or `FAIL_CLOSED` with its diagnostic.

   Provider-scoped `401`, `402`, `403`, and `429` failures fence that provider for the
   remainder of the operation and immediately advance to the next eligible provider; do not
   burn attempts on sibling models from the same failed provider. Model-specific failures,
   transient transport failures, malformed results and timeouts remain isolated to the failed
   attempt according to runtime health policy. Every replacement receives the same prompt.
   Children must both send an explicit reply and finish with a nonempty assistant final
   response. A preview, early reply, or admission handle never completes a task. Failed writers
   are deleted before replacement; unconfirmed cleanup fails closed. The attempt bound is the
   unique eligible pool, optionally reduced by an explicit operation budget. Probes and attempts
   share the total deadline.
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
   and only then retry. Capture the failure and retained dirty diff, increment attempt, call
   hydrate-context again, then choose an eligible target anew. A worker awaiting input is a
   surfaced blocker, not idle success. Never exceed the guide's recovery budgets or steal an
   external writer.
