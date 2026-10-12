# Design: Native worker context adapter

## Context

The source path is `orchestration.run.prompt_for` -> `planner.hydrate_node_prompt` -> worker executor. Runtime already narrows the byte allowance after overflow. Existing Core context intelligence and compiler contracts provide native retrieval and safety. No external semantic adapters need to be healthy for this slice.

## Goals / Non-Goals

Deliver a usable node pack on the production hydration path now. Retain ownership and verification instructions. Bound excerpts and prompt context, label unknowns and preserve fallback. Do not authorize routes, fabricate health, import external adapters, dump transcripts or claim live self-hosting. Automatic selected-window propagation, new event schemas, live adapter execution, persistent pack storage and total scan-work bounds are not goals of this increment.

## Decisions

1. Planner always calls `build_worker_context`; this is not a helper-only feature. Only one WorkNode enters the adapter. The global goal is not a retrieval query, so sibling contracts are not injected.
2. Reuse native definition/reference/test-impact/docs/symbol capabilities through existing query execution. The default capability registry remains unchanged and optional adapters stay unavailable. It describes availability; it does not execute or certify external tooling. Wrap native providers so parser failure becomes `retrieval_error`; if query execution fails, required excerpts remain the native fallback.
3. Extract at most eight required/owned file excerpts, each at most 2048 UTF-8 bytes and forty lines, around the first target-symbol match. Native results are also excerpt-capped. Required excerpts win compiler ordering. Unknown source revision remains `unknown`; exact spans are verified against source text or explicitly omitted. Each rendered citation includes excerpt SHA-256, observation time, authority and trust.
4. Use the existing compiler for scope, secret/injection handling and duplicate suppression. Count full rendered context bytes and estimated tokens. Remove whole units if rendering exceeds the allowance. Context tokens are capped at 4096 and the shrinking byte allowance divided by four. Optional candidate plan additionally caps remaining input after output/tool reserves and mandatory prompt tokens. Mandatory instructions themselves are never truncated; if they exceed the model window, context is omitted, not falsely declared fitting.
5. Do not retrieve memory or history. No memory writes bypass MemoryGate because this adapter performs no writes at all. This avoids persisting prompts or excerpts in the first slice. Future memory support must use MemoryGate and reviewed fact-only records, not transcript payloads.

## Risks / Trade-offs

Native providers scan repository Python/docs and their graph engine may traverse unrelated source. Output is bounded; scan-time and all internal discovery reads are not newly bounded. Explicit path sources and final rendered units are constrained to the worktree. Caller/test graph citations can have synthetic `code://` sources rather than exact file spans. Token counts use Core's deterministic estimate, not a provider tokenizer. Small budgets can omit all evidence. Existing runtime hydrate events describe legacy file-prefix accounting, not actual pack provenance; prompt citations are authoritative for this increment. Fallback status is explicit but cannot guarantee enough evidence for the task.

## Security / trust implications

Pack content is untrusted quoted evidence. The worker prompt labels it advisory and never route authorization. Secret rejection occurs on whole read content before excerpting and again before rendering. Exception messages are never emitted. Dotfile/transcript/outside-symlink content is excluded. Existing compiler injection transforms remain active. Unknown revision is not a verified commit identity. This is a heuristic safety boundary, not proof that arbitrary source is secret-free.

## Routing / context / memory implications

No changes to candidate admission, ranking, route membership or health evidence. The optional `context_plan` narrows context only. Without a production selected plan, candidate identity/window are unknown and the fallback is labeled conservative. No durable memory connections or writes occur, and no raw transcript is retrieved. The existing context-lift required-fact proof helper is not repurposed as worker authorization or evidence of live lift.

## Migration / rollback

No schemas, dependencies or durable state migrate. Existing callers keep the same positional/keyword requirements; `context_plan` is additive and optional. Reverting planner wiring and removing the adapter restores file-prefix hydration without rewriting historical receipts. Coordinator follow-up can forward selected ContextPlan and record actual pack sources, but requires separately scoped runtime/run work.

## ADR impact

This implements ADR-0001's Core-owned native context baseline and ADR-022's advisory/unavailable separation on the ADR-036 worker path. No new route authority, retry owner or memory plane is introduced. No new ADR is required for this incremental adapter.
