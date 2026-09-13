# Prime workflow hardening

## Objective
Harden the existing project-owned Prime workflow so a fresh `/verdict-resume` process can safely recover bounded issue delivery without conversational memory or uncontrolled writers.

## Requirements
- Durable checkpoints reconstruct issue state and next action.
- Per-issue leases fence stale writers by generation.
- Receipts bind dispatch, worker, progress and lease evidence.
- Watchdog progress ignores process liveness and unregistered artifact churn.
- Proof and CI failures fail closed and route to bounded corrective action.
- Prime project-local discovery and compaction preserve canonical continuity state.

## Non-goals
No new orchestration engine, memory database, model router, Linear client, GitHub client, or Spec Kit replacement.
