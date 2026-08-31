# Implementation Plan: Context Intelligence Lift

**Branch**: `feat/272-p3-context` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/336-context-intelligence-lift/spec.md`

## Summary

Compile a **model-aware pack from slice-selected docs, code, and durable memory**, inject it as typed slots (not a chat dump) onto a **cheaper live identity**, and **measure lift** with a paired named check. Fixture-only scoring is not a pass.

Reuse `ContextPackCompiler`, `MemoryPlane`/`MemoryGate`, documentation inventory, `CodeGraphEngine`, Feature 276 live cheaper-first selection, and evaluation variant vocabulary (`no_context` vs `context_pack`). Do not copy ADK or require Vertex.

## Technical Context

**Language/Version**: Python 3.10+ as used by `verdict-core`

**Primary Dependencies**: Existing `verdict-core` (uv, pytest, httpx). Live OpenAI-compatible gateway for the paired proof. No new product repo. No Vertex/ADK vendor dependency.

**Storage**: Existing local MemoryPlane (SQLite + FTS5) for durable archive. Working state is in-memory typed slots for one task. No new database.

**Testing**: pytest. Retrieval/compile/gate tests use local planted files and an isolated MemoryPlane. Live paired tests hit a real cheaper identity; unreachable gateway is blocked/skip, not passed.

**Target Platform**: Linux operator workstation with a reachable gateway (default OmniRoute `http://localhost:20128/v1`).

**Project Type**: Library + callable orchestrator in `verdict-core`

**Performance Goals**: One paired named check; pack compile offline and deterministic; live attempts bounded (seconds to a few minutes).

**Constraints**: Core owns policy. Adapters never admit/exclude. Fail closed. No secrets in packs/receipts. No whole-repo dump. No vendor memory required. Deterministic slices. Same cheaper identity for unaided and packed.

**Scale/Scope**: One cheaper concrete identity, one planted unique fact, three source categories (docs, code, memory), one paired evaluation.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- Coordination is not execution: live cheaper identity and independent checker required for SC-004; memory/handoffs do not count as lift.
- Documentation before dependencies: ADK Session/MemoryService and existing Verdict pack/memory modules were read; Vertex is not required.
- Repository boundaries: implementation is this `verdict-core` worktree only.
- Verification is part of the change: pytest plus a live paired run; blocked ≠ passed.
- Least authority: gated ingest; receipts allowlisted; no credential persistence.

Post-design: still pass. Paired live proof increases verification honesty rather than weakening gates.

## Project Structure

### Documentation (this feature)

```text
specs/336-context-intelligence-lift/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/context-intelligence.v1.md
└── tasks.md
```

### Source Code (`verdict-core`)

```text
verdict/context_intelligence.py    # NEW: slices, retrieve, compile, working state
verdict/context_lift.py            # NEW: plant fact, paired live eval, receipt
verdict/context_pack.py            # reuse compiler; do not replace
verdict/memory_plane.py            # reuse search/ingest
verdict/memory_gate.py             # reuse gated writes
verdict/documentation_preflight.py # reuse ADR/docs discovery
verdict/code_graph.py              # reuse bounded symbol retrieval
verdict/live_routing.py            # reuse cheaper-first identity selection
verdict/live_routing_gateway.py    # reuse catalog fetch; add generic execute helper
tests/test_context_intelligence.py # planted-source rule tests
tests/test_context_lift.py         # pairing/checker/receipt rules
tests/test_context_lift_live.py    # live paired eval; blocked if down
```

**Structure Decision**: Extend `verdict-core`. Isolated git worktree `feat/272-p3-context`. Do not add a service or vendor SDK.

## Complexity Tracking

No constitution violations.

## Phase 0 / Phase 1

See [research.md](./research.md), [data-model.md](./data-model.md), [contracts/context-intelligence.v1.md](./contracts/context-intelligence.v1.md), [quickstart.md](./quickstart.md).

Named check: live completion whose body must be exactly JSON `{"lift_fact":"<exact planted token>"}`. Independent checker: parse JSON; pass only if `lift_fact` equals the planted token.

Slice planning: deterministic from the task and default source locations (`docs/adr`, local code under the proof root, MemoryPlane search). Optional stronger-identity proposals are out of the live proof.

Working state: typed slots only. Durable memory: gated ingest + FTS search.

Live identity: Feature 276 cheaper-first (`local` < `free` < `cheaper`). Paid is not the lift subject while a cheaper unused qualified identity remains.
