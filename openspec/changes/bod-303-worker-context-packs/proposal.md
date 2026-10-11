# Proposal: BOD-303 first native worker context slice

## Why

**KNOWN**: `run.py` already calls `planner.hydrate_node_prompt` on each worker attempt. At base `e1645b05` the hydrator copies file prefixes. Core already owns native semantic retrieval, a capability registry, injection-safe compilation and source provenance. These mechanisms were not connected to worker hydration.

## What Changes

- Wire the existing worker hydrator to a narrow advisory pack built by `worker_context.py`.
- Seed native definition/reference/test-impact/design retrieval from this node's objective, acceptance and required/owned files. Prefer required contracts, tests and design excerpts, then bounded native units.
- Render source, exact excerpt span when known, excerpt digest, observation time, trust, authority and explicit unknown revision in citations. Treat retrieved text as quoted evidence, not instructions.
- Reuse `ContextPackCompiler`, `plan_context_query`, `execute_context_query`, native providers, registry defaults and UTF-8 truncation. Do not duplicate routing or memory policy.
- Bound the entire context projection (citations and metadata included) by bytes and deterministic estimated tokens. Support optional `ContextPlan` with window/reserves and mandatory prompt accounting. Without that plan, record `conservative_unknown_candidate`; cap context at 4096 estimated tokens.
- Fail to named gaps on provider/query errors and retain safe required excerpts. Reject secret-bearing content before slicing, outside symlinks, dotfiles and transcript sources. Open no durable memory and write no memory.

## Capabilities

### New Capabilities

- `orchestration-worker-context`: Native node-specific advisory packs on the existing production worker hydration path.

## Impact

**KNOWN**: Changes own only planner, a new worker-context adapter, focused tests, this OpenSpec change and a behavior document. Runtime/run/eligibility and existing context modules remain unchanged. No installs, live calls, gateway changes or credential access.

**INFERRED**: Narrow packs should help a worker locate definitions, callers, tests and design without file-prefix padding. Fixture tests demonstrate wiring and bounds, not task quality or savings.

**NOT AVAILABLE**: A live self-hosted COMPLETE run is not available. Current local configured keys block live orchestration. Automatic selected-window forwarding and truthful per-unit hydrate event sources are a named second increment owned by the coordinator. Native discovery still traverses the repository; retrieved/compiled units are bounded, but total repository scan work is not newly bounded here.

## Acceptance Criteria

- [x] Production planner hydration and a scripted runtime dispatch contain target definitions, callers, contracts/tests/design and source citations.
- [x] Byte/token budgets include citation/metadata overhead, UTF-8 stays valid, and candidate plans count reserves plus mandatory prompt tokens.
- [x] Unknown selected window remains explicitly unknown with conservative fallback provenance.
- [x] Native errors retain safe required excerpts with named gaps and never echo exception text.
- [x] Secrets are rejected before slicing; transcripts, dotfiles and outside symlink content never reach the pack. No durable memory is opened.
- [x] Focused planner/adapter tests, lint/type checks and OpenSpec contract validation pass. None constitutes live proof or whole-story completion.
