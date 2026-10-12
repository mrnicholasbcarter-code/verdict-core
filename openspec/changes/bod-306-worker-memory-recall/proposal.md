# Proposal: BOD-306 first slice: worker packs recall gated memory facts

## Why

**KNOWN**: Each worker pack (`worker_context.build_worker_context`, called from `planner.hydrate_node_prompt` on every attempt) is built from code, tests and docs only. Decision 5 of `bod-303-worker-context-packs` left memory out and said future support must use MemoryGate and reviewed fact-only records. `MemoryGate` already writes verified records into the `decisions`, `feedback`, `patterns` and `evidence` namespaces of the local MemoryPlane (`~/.verdict/memory.db`, ADR-004/009). `NativeMemoryProvider` already serves `memory.search`. No worker reads either today, so a decision recorded in one run never reaches the worker of the next.

**INFERRED**: BOD-306 asks the queen to recall bounded, cited prior context for each worker. Gated facts are the safe first source. Ingesting sessions, review findings and CI results is a later increment.

## What Changes

- Worker packs add `memory.search` as the last capability. Only active, unexpired, gate-verified records are recalled: from the four fact namespaces, with `public`, `standard` or `internal` sensitivity, at most two units.
- The recall query is built from this node's objective, acceptance criteria and target symbols only, never from the global goal.
- Each recalled fact is redacted with `verdict.security.redact_text`. It is cited by its record id, excerpt digest, authority, trust and observed time, and appears in `WorkerPrompt.evidence()` sources.
- `NativeMemoryProvider` accepts optional `namespaces`, `sensitivities` and `verified_only` hints. Without hints its behaviour is unchanged.
- The plane is opened read-only in effect: nothing is written, and a missing plane is never created. A missing or unreadable plane becomes a named omission.

## Acceptance criteria

- A gated decision or operator correction that matches the node appears in the worker prompt with a `memory:<record_id>` citation, after the code evidence.
- Unverified, confidential, transcript, session-history and unrelated records never appear.
- A missing or broken plane is a named omission. No plane is created and nothing is written.
- A credential in a recalled fact is rendered as `[redacted]`.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `orchestration-worker-context`: packs recall gated memory facts; transcripts and history stay excluded.

## Impact

**KNOWN**: Owned files are `verdict/orchestration/worker_context.py`, `verdict/context_sources.py`, `tests/test_orch_worker_context.py` and this change. Routing, admission, MemoryGate policy, the outbox and the shared provider are unchanged. There are no installs, no live calls and no schema changes.

**INFERRED**: Workers see earlier operator decisions and corrections for the code they touch. The units are ranked after code evidence, so the compiler drops memory first when the budget is tight.

**NOT AVAILABLE**: The improvement in review rounds or `context_lift` is not measured. The operator's current plane holds only `authoritative-docs` and `documentation-manifest` records, so recall returns nothing until gated facts are written. Session, review-finding and CI-result ingestion (the rest of BOD-306) and shared-provider recall (BOD-336) are not part of this slice.
