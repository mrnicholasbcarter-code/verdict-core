---
name: hydrate-context
description: Use when a Verdict issue is dependency-READY and immediately before creating or resuming its execution attempt.
---

# Hydrate context

Read `docs/guides/prime-workflow.md`. Produce the version-1 packet described there.
HYDRATED means this packet is current and complete, not merely that documents were read.

1. Re-fetch the selected Linear objective, acceptance criteria, priority, relations and
   revision timestamp. Recheck all blockers. Record issue URL, project/team, and source times.
2. Inspect git worktrees and issue branches. Select exactly one existing compatible worktree,
   or record the planned path/branch and fetched main base SHA for dispatch to create.
   Never switch another session's checkout. Record current SHA, dirty status and ownership.
3. Query configured codebase-memory/code-review-graph before code search. Read relevant
   architecture, ADRs, Spec Kit spec/plan/tasks, code and tests. Use Basic Memory/context
   providers as retrieval aids with provenance; verify against source. Record unavailable,
   stale or omitted context explicitly. Use Sequential Thinking for difficult dependency or
   architecture reasoning if useful; its conclusions are not proof.
4. Use the existing `documentation-and-adrs` skill when decisions or public interfaces change.
   Add required documentation/ADR tasks and references to acceptance criteria. Reuse installed
   Spec Kit hooks and project conventions; do not clone the global skill catalog.
5. Define bounded file scope, architecture/privacy/security constraints, exact allowed commands,
   model budget, ten-minute progress and one-hour hard deadline defaults, two recovery attempts,
   the `attempt_id` that becomes the lease `dispatch_id`, the lease staleness bound, heartbeat and
   progress expectations (what counts as substantive progress), and the required proof/return
   evidence. STATIC, UNIT, INTEGRATION, ACCEPTANCE-PROOF must each
   be classified required or N/A-with-reason before execution. Every acceptance criterion has
   an observable verification method; ACCEPTANCE-PROOF cannot be N/A.
6. Assemble `packet.json` with all guide fields and compact source excerpts/references. Select
   target through existing Verdict routing/chooser capability, using fresh availability and
   eligibility evidence; dispatch rechecks it. Never invent an alias or choose solely by cost.
7. Run `validate_packet` from `scripts/prime_workflow.py`; atomically persist it and checkpoint HYDRATED. Dispatch must
   reject a changed issue revision, base SHA, target availability or worktree ownership and
   call this skill again. A previously read conversation is never the dispatch packet.
