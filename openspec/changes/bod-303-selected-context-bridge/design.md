# Design: Selected candidate context and truthful hydration evidence

## Context

Production run.prompt_for calls planner.hydrate_node_prompt. DagRuntime already binds RouteVerdict from selector.select and halves the context byte allowance after a context-length failure. RouteVerdict.context_window is populated by eligibility from max_input_tokens or context_length, but does not expose which inventory field supplied it. No private gateway database is needed or consulted.

## Goals / Non-Goals

Deliver the next narrow production bridge and preserve first-slice pre-aggregation safety. Keep admission/ranking unchanged. Do not claim an exact provider total window, install a tokenizer, invent revisions, persist packs or run models. Do not change controller hydration, recovery ownership or executors.

## Decisions

1. Record the chosen verdict's window on the node before dispatch; never take another considered route's window. Invalid/missing windows remain unknown. ContextPlan candidate_id is the actual selected route.
2. The observed catalog window is a conservative ceiling, not a claim of a verified combined input/output window. Eligibility normalizes input-only and total-shaped inventory into the same public field. Narrow by 1024 output and 512 safety/tool tokens once, not twice. This intentionally underuses an input-only window rather than inventing its semantics. Unknown windows use a local total budget target of 8192 with candidate_context_window=None. The byte cap additionally narrows the input to budget_bytes // 4. Tiny windows retain their actual token_budget; ContextPlan reserves are clamped only to remain representable, while reserve_policy reports shortfall and runtime still enforces the full policy.
3. The production callback returns WorkerPrompt. hydrate_node_prompt defaults to str and provides an optional structured flag. Runtime passes only supported context_budget_bytes, context_plan and mandatory_suffix keywords, including **kwargs adapters. Legacy callbacks returning strings have sources_status=unknown_legacy_callback and no fabricated sources.
4. Measure the full mandatory text plus retry suffix before packing. Subtract its estimated tokens and UTF-8 bytes. Account for the inserted separator. Remove whole context units through the existing pack loop. Do not truncate mandatory rules or feedback. Verify final full prompt estimated tokens and UTF-8 bytes before executor launch. A local context_input_rejected failure blocks without route cooldown or admission widening.
5. Hydrate evidence comes only from selected ContextPack units/slots. Record unit IDs, source URI, raw excerpt digest/span, explicit revision/observation/trust/authority, rendered-content digest/bytes, estimated tokens and compiler transforms. Record pack_id, omissions, exact candidate plan/reserves, prompt measured bytes and estimated tokens. prompt_tokens_measured=None and tokenizer=unknown:offline_four_char_estimate are explicit. No source prefix reads occur in production events. Legacy accounting helpers remain only for historical tooling.

## Risks / Trade-offs

Core's four-character estimate is not a provider tokenizer or a mathematical upper bound. Hidden harness system/tool overhead is not observable; the 512-token reserve is policy, not measured proof. Small windows can omit all context or block mandatory text. Native scan work remains unbounded by this bridge. ContextPlan's normalized window cannot establish input versus total semantics. Historical string callbacks cannot report actual pack provenance. Source revisions remain unknown, and synthetic code:// aggregate spans can be unknown. Compiler-sanitized rendering has its own digest and transform reason; the excerpt digest identifies the pre-transform quoted excerpt.

## Security / trust implications

All first-slice safe path, whole-source secret screening, pre-aggregation native filtering and exact trusted aggregate identity checks remain intact. Context stays quoted/advisory. No transcript/memory reads or writes, credential access, new adapter health assumptions or exception payloads are introduced.

## Routing / context / memory implications

Admission is unchanged. Selected verdict metadata only narrows request hydration. A local input non-fit does not mark provider health or select a more permissive route. Worker context writes no memory and authorizes no model.

## Migration / rollback

No durable/schema migration or dependencies. Existing hydrator callables keep their positional contract and optional supported keywords. Existing hydrate event fields remain with additive actual-pack fields; sources now means selected units, not file-prefix guesses. Revert this bridge to restore the previous run callback without changing historical receipts.

## ADR impact

The bridge applies ADR-0001 advisory boundaries and ADR-036's runtime worker path. It creates no new authority, retry owner or memory plane, so no new ADR is required.
