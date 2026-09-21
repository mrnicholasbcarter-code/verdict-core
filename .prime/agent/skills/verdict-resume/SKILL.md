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

## Job-ready mission resume (2026-09 hiring-lane effort)

When resuming the Verdict job-ready flagship mission (model/context selection
architecture S1-S4, BOD-133 residual, live proofs, recruiter demo closure):

1. Read `$(git rev-parse --git-common-dir)/verdict-prime/mission/STATUS.md` FIRST.
   It is the durable working-status handoff: what merged, what is in flight,
   branches/worktrees, decision log, environment pitfalls, next work order.
   Update it on every key decision, discovery, merge, or blocker.
2. Supporting mission state lives beside it: `story-decomposition.md` (derived
   stories + audit findings), `linear-issues.json` / `linear-relations.json` /
   `linear-s-stories.json` (Linear snapshots + created story ids),
   `live-route-targeted.log`, `targeted-passport-proof.json`,
   `live-passports.json`, `baseline-security-demo.log`.
3. Mission Linear stories: S1=BOD-140 (In Progress), S2=BOD-142, S3=BOD-143,
   S4=BOD-144, chained blocks S1->S2->S3->S4, linked to epic BOD-132. Verify
   live before trusting; update each story's "Current state" section as work
   moves (implemented/review/PR/merged/proof), then reconcile state.
4. Commit AND push each story checkpoint as soon as gates pass so work survives
   session loss; branches are pushed even before review/PR.
5. Preserve the dirty BOD-136 WIP in the main worktree and all
   `/home/nick/worktrees/*` branches; never absorb or delete them.

## OmniRoute gateway notes

- The gateway (`http://127.0.0.1:20128`) runs with compression settings
  enabled (semantic/RTK-style compression layers). Treat gateway-returned
  content as possibly compressed/transformed: verification against exact
  expected strings can fail for gateway reasons, and proof prompts should ask
  for short exact tokens. Do not attribute gateway compression artifacts to
  Verdict routing bugs without a direct transport comparison.
- Gateway latency varies widely: 3s probe timeouts fail the whole catalog while
  the same target answers in <1s with a 30s timeout. For live story proofs use
  targeted single-identity probes with >=30s timeout, not full-catalog prove.

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

## Autonomous execution rules (always-on)

- Do not ask routine questions ("should I continue?", "open a PR?", "run tests?"). Make a documented engineering ruling and continue when safe and within authorization.
- Only stop for: missing credentials/MFA you cannot obtain, payment/billing, unavoidable sudo/root, genuinely irreversible destructive owner decisions, or a product decision where every reasonable path would materially change intended behavior.
- Use bounded concurrent subagents only when machine headroom supports them. With single-CPU / low-RAM hosts, prefer direct controller implementation over spawning competing writers.
- Preserve all unrelated WIP. Before deleting any worktree or branch, prove it is merged or disposable and inspect untracked files. The owner's `wip/pre-wave-existing-state` branch must never be deleted, rebased, or absorbed.

## Transport-reuse design gate (protocol adapter work)

- Prefer a thin protocol/intercept boundary that preserves Verdict's authoritative strategy, context, qualification, admission, receipt/provenance, and verification contracts.
- Reject full provider protocol duplication inside Core unless no qualified gateway adapter path can preserve the required semantics/evidence.
- For Anthropic /v1/messages: implement a minimal subset adapter (messages, streaming, buffered JSON, client tool definitions, tool-use/result blocks) rather than a complete Messages stack. Deny unsupported capabilities explicitly; never silently strip or relabel OpenAI qualification as Messages qualification. See `references/transport-reuse-adapter.md` for the design table.

## Resource-aware verification

- When full CI/full-suite verification is blocked by resource limits, targeted unit/proof tests on the changed interface are sufficient evidence — provided they include both passing and failing paths, cover auth/qualification conflicts, secret stripping, stream termination, and regression on existing surfaces.
- Isolate test subprocesses from inherited production auth tokens (`env -u` or equivalent) so tests run in fixture-owned environments without changing application auth behavior.
- Never present synthetic fixtures as live evidence; never invent benchmark savings.

## Linear reconciliation hygiene

- Linear is planning truth but may be stale. When the Linear connector/plugin is unavailable (e.g., `hermes plugins list` shows no `linear` plugin), use the Composio CLI (`composio execute LINEAR_SEARCH_ISSUES`) as the authoritative live planning source. Always treat historical Linear snapshots (`.verdict/previous-linear-evidence.txt`) as hints, not truth — verify against live `LINEAR_SEARCH_ISSUES` output before marking Done or closing superseded items.
- A Linear Todo does not prove work is missing. A Linear Duplicate (e.g., BOD-22 superseding legacy LiteLLM plugin; BOD-13/21 superseding benchmarks) confirms supersession but requires the canonical issue to be verified Done before closing. A Linear Canceled (e.g., BOD-79, BOD-105, BOD-74, BOD-56, BOD-62) confirms obsolete architecture — never resurrect canceled swarm/SONA/Ruflo work.
- If the Linear connector is unavailable, record the omission explicitly in evidence and never treat historical Linear snapshots as live status.
- For superseded/stale items: if fully implemented, mark Done only after code/test/proof verification. If duplicate, identify the canonical issue. If superseded (e.g., by BOD-104, BOD-124, BOD-131), cancel/close with explanation and update Linear relations.
- If a story is blocked (e.g., BOD-101 blocked on BOD-106 `pack_state=hydrated`), verify the blocker against current code/PR evidence rather than trusting stale Linear labels.
