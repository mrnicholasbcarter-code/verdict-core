# Research: Context Intelligence Lift

**Feature**: `336-context-intelligence-lift`  
**Date**: 2026-08-30

## R1. ADK Session/state vs MemoryService (do not copy ADK)

- **Decision**: Map ADK concepts onto existing Verdict primitives. Do not depend on `google-adk` or Vertex.
  - Session/state → **working state**: typed slots for the current task (goal, slices, retrieved units, pack digest).
  - `BaseMemoryService.add_session_to_memory` / `search_memory` → **MemoryPlane put + search_ranked** behind **MemoryGate**.
  - Compaction/summarizer → optional, model-aware omit/summarize with provenance; not a dump.
  - `output_key` / state injection → typed slot keys, not chat-history dump.
- **Rationale**: [ADK memory docs](https://adk.dev/sessions/memory/) and [session vs memory](https://adk.dev/sessions/) separate short-term conversation from a searchable archive. `BaseMemoryService` requires `add_session_to_memory` and `search_memory`; Vertex Memory Bank / RAG are optional implementations, not the interface. Copying ADK would violate “do not require Vertex/ADK as a vendor” and Core-owns-policy.
- **Alternatives considered**: Embed `InMemoryMemoryService`. Rejected: duplicates MemoryPlane, no provenance/gate. Require Vertex Memory Bank. Forbidden.

Sources: https://github.com/google/adk-python `src/google/adk/memory/base_memory_service.py`; https://adk.dev/sessions/memory/.

## R2. Existing Verdict primitives (reuse, do not rewrite)

- **Decision**: The gap is an orchestrator that **retrieves → compiles a model-aware pack → measures cheaper-model lift**. Do not replace:
  - `ContextPackCompiler.compile_units` (offline, injection-safe, budgeted, decisions on receipt)
  - `ContextEnvelope` / `ContextCompiler.optimize_for` (policy/goal never drop)
  - `MemoryPlane` SQLite+FTS5 with provenance, staleness annotation
  - `MemoryGate` (secrets/transcripts refused; adapters never own policy)
  - `documentation_preflight.discover_sources` (ADRs under `docs/adr`)
  - `CodeGraphEngine` (bounded symbol/file queries; `sync_to_memory_plane`)
  - Feature 276 `live_routing` cheaper-first select + live execute
  - `evaluation.EvaluationVariant.NO_CONTEXT` / `CONTEXT_PACK` for pair vocabulary
- **Rationale**: Spec 272 FR-011/FR-032 already require a bounded worker package with omissions named. Compiler already excludes secrets and expired/out-of-scope units. Evaluation already forbids scoring transport failures as quality.
- **Alternatives considered**: New RAG stack. Rejected: vendor lock and dump risk. New memory DB. Rejected: MemoryPlane is the archive.

## R3. Spec 272 Phase 3 remainder

- **Decision**: Implement the Phase 3 exit signal only: paired evaluation of context packages on at least one cheaper/alternative model. Phases 4–5 (learning, swarms) stay out of scope.
- **Rationale**: 272 Phase 3 user-visible outcome is a provider-neutral Context Intelligence Plane with hybrid retrieval, trust/freshness, model-aware compilation, progressive disclosure, shared docs/memory recall. Boundary: no whole-repo dump; no particular RAG/graph/vector vendor.
- **Alternatives considered**: Full hybrid semantic+graph RAG. Deferred; lexical FTS + deterministic file/symbol slices are sufficient for the planted-fact proof.

## R4. Feature 276 live cheaper-first execute

- **Decision**: Obtain the lift subject from 276’s live path: fetch `/v1/models`, classify from published specs, select `local` then `free` then `cheaper`. Do not use paid while a cheaper unused qualified identity remains. Do not treat fixture catalogs as the demo.
- **Rationale**: 276 already proved cheaper-first live execute (`verdict/live_routing.py`, `live_routing_run.py`). This feature must not re-prove catalog discovery; it must prove packing changes cheaper-model success.
- **Alternatives considered**: Hard-code a free model id. Rejected: name guessing. Use 276’s `{"golden_path":"ok"}` check. Rejected: that task is guessable without retrieved context.

## R5. Named check and planted fact

- **Decision**: Plant a synthetic high-entropy token in local docs and/or durable memory (and a small code marker). Unaided prompt describes the JSON shape but MUST NOT contain the token. Packed attempt includes retrieved units that contain the token. Checker: `json.loads(body.strip()) == {"lift_fact": "<token>"}`.
- **Rationale**: Unique token is not in model weights, so unaided should fail and packed can succeed by copying from a slot. Matches 276’s exact-JSON checker discipline.
- **Alternatives considered**: Open-ended coding task. Rejected: not independently checkable. Include token in the unaided prompt. Rejected: would make unaided succeed without the pack.

## R6. Slice planning is Core-owned and deterministic

- **Decision**: `plan_slices` derives bounded lookups from the task and default locations (ADRs under `docs/adr`, code files under the proof root matching query terms, MemoryPlane search). A live reasoning model is not required. Slices that would glob the whole repo are refused.
- **Rationale**: Reproducible packing (FR-022) and no second live model as a hidden dependency. ADK `output_key` is the analogue of typed slots, not of letting the model dump history.
- **Alternatives considered**: Always call a frontier model to plan slices. Rejected for the live proof: extra spend, non-determinism, vendor-shaped dependency.

## R7. Fail closed, no secrets, no dump

- **Decision**: Missing required fact or unknown cheaper-identity context limit → refuse pack / block live proof. MemoryGate already redacts secrets and prompt/transcript keys; pack compiler excludes secret-shaped content. Retrieval caps file count and bytes; never attach a repository root as one unit.
- **Rationale**: Constitution V and spec FR-005/FR-009/FR-010.
- **Alternatives considered**: Truncate the required fact to fit. Forbidden. Best-effort pack without the fact. Forbidden.

## R8. Paired lift reporting

- **Decision**: Conclusion classes: `lift` (unaided fail + packed success), `no_lift` (both succeed or both fail on the checker), `blocked` (live surface, no cheaper identity, unknown context limit, compile refuse). Transport/quota errors are `blocked`, never quality and never lift. Invalid if identities differ or either attempt is a fixture stub.
- **Rationale**: Aligns with `evaluation.py` (do not score AUTH/TRANSPORT/QUOTA as quality) and 272 Phase 3 exit signal.
- **Alternatives considered**: Claim lift when packed “looks better.” Rejected: not independently checkable.

## R9. Optional compaction

- **Decision**: P3. Default off. If enabled, lower-priority units may be shortened with action `summarize` recorded; required policy and the planted fact stay verbatim. No extra model call required for v1 (extractive trim is enough).
- **Rationale**: Honest omission already satisfies US1–US3. ADK event compaction is a summarizer over chat events; Verdict must not compact by dumping then summarizing the repo.
- **Alternatives considered**: Always LLM-summarize retrieval. Rejected: spend, nondeterminism, secret leakage risk.
