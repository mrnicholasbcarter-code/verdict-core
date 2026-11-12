# Proposal: BOD-306 memory-fed brief and session-recall contract

## Why

**KNOWN**: The 2026-10-26 delivery scope for BOD-306 (Linear issue BOD-306, kanban `t_ac768f38`) requires cited ai-memory in compiled briefs, continuity across interruption, stale-source rejection, and a new-dispatch block when mandatory memory infrastructure is missing. On checkout `a12e5dda59c9632fe83c6d726b3c58ce5bac9cf2`, planner briefs recall only gated local MemoryPlane facts, autodev `compile_unit_context` does not query memory, and no production caller authenticates ai-memory into those briefs.

**INFERRED**: Later implementation cards will re-interpret policy unless this change freezes fields, file ownership, tests, and the drill matrix.

**NOT AVAILABLE**: This change is not implementation, not a Linear status change, and not a claim that BOD-306 is Done.

## What Changes

- Freeze the compiled-brief record: cited task, project, and session decisions and lessons, source revisions, and explicit `empty` versus `unavailable`.
- Freeze the continuity packet: incremental research, packet revisions, changed files, last proof, and exact next action. Fence the old writer before replacement. Resume on the current eligible-route authorization with the same governing task. Refresh only changed or missing sources.
- Freeze stale rejection: a changed acceptance criterion, spec, or ADR rejects stale continuation and requires affected rehydration.
- Freeze project isolation, secret scanning, supersession, durable capture through the existing outbox, and idempotent replay.
- Freeze the rule that memory text cannot approve policy, weaken tests, or certify Done.
- Freeze the dispatch rule: missing mandatory ai-memory infrastructure blocks new affected dispatch; in-flight work stays alive on the existing durable state and capture contract.
- Partition files so the brief slice and the continuity slice do not edit the same files.
- Record unresolved execution-pack choices as blockers. This change does not decide them.

No production code changes in this change's authoring task. Implementation tasks below stay open.

## Capabilities

### New Capabilities

- `memory-fed-brief`: cited ai-memory fields in the compiled planner and autodev brief, with `empty` distinct from `unavailable`.
- `session-continuity-packet`: interruption and root-loss continuity for one governing task.
- `memory-dispatch-guard`: stale rejection, isolation, and the new-dispatch block.

### Modified Capabilities

- None. `openspec/specs/` has no archived memory-brief capability. The checked change `bod-306-worker-memory-recall` remains the local MemoryPlane slice and is not rewritten here.

## Impact

**KNOWN**: Affected readers are `verdict/orchestration/planner.py:hydrate_node_prompt`, `verdict/orchestration/worker_context.py:build_worker_context`, `verdict/autodev_run.py:compile_unit_context`, `verdict/orchestration/run.py:run_golden_path`, `verdict/worker_runtime.py:_worker_admission` and `cli_run`, and `verdict/orchestration/supervisor.py`. The shared consumer to call, not replace, is `verdict/context_sources.py:SharedMemoryCapabilityProvider`. Durable state already on disk is `events.jsonl`, `graph.json`, and `progress.json`, plus `verdict/memory_outbox.py:MemoryOutbox`.

**INFERRED**: Workers that follow the file partition can land the three slices without editing one another's files.

**NOT AVAILABLE**: Whether ADR-033 and ADR-034 will be superseded. Whether PR 986 will merge before the brief slice starts. No new cap, timer, or restart budget is authorized by this proposal.

## Acceptance criteria

1. Planner and every new affected worker brief carries cited task, project, and session decisions and lessons, source revisions, and a status that is exactly one of `cited`, `empty`, or `unavailable`. `empty` and `unavailable` are never the same result.
2. A continuity packet retains incremental research, packet revisions, changed files, last proof, and exact next action across interruption and root loss for the same governing task. The old writer is fenced before replacement. Resume uses the current eligible-route authorization. Only changed or missing sources are refreshed.
3. A changed acceptance criterion, spec, or ADR rejects stale continuation and requires affected rehydration. Project isolation, secret scanning, supersession, the existing durable capture outbox, and idempotent replay still hold. Memory text cannot approve policy, weaken tests, or certify Done.
4. Missing mandatory ai-memory infrastructure blocks new affected dispatch. Existing running work remains alive under the existing durable state and capture contract.
