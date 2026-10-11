# BOD-303: Selected worker context bridge

This is the second bootstrap-tested increment, based on first native slice `33c54d35`. It is not live self-hosted COMPLETE proof or whole-story completion.

## Production path

`run.prompt_for` -> `planner.hydrate_node_prompt(structured=True)` -> `WorkerPrompt` -> `DagRuntime` -> worker executor. Runtime constructs ContextPlan from the exact `selector.select` choice. Missing context_window remains unknown. No private gateway inventory is read.

The plan narrows the request by the observed catalog ceiling, 1024 output tokens, 512 safety/tool tokens and the shrinking byte allowance. Eligibility may have sourced context_window from max_input_tokens or context_length. This bridge does not know which. Treating both as a conservative ceiling underuses input-only windows; it does not assert a verified total input/output capacity. Reserves are charged once. Unknown windows use a local 8192 total target, not a fabricated observed window.

Mandatory goal/objective/acceptance/ownership/verification/rules plus retry feedback count before packing. The production full prompt must fit both UTF-8 bytes and the offline input estimate. Whole evidence units shrink; mandatory rules never do. A non-fit emits context_input_rejected and blocks locally without executor launch or provider cooldown. This does not change admission authority.

## Evidence and compatibility

Hydrate events record actual selected pack units, unit IDs, excerpt digests, spans, unknown revisions, observation/trust/authority, rendered-content digests and compiler transforms. They also report omissions, pack/route/budget facts, full measured bytes, explicit estimated tokens and unknown provider tokenizer. No independent file-prefix reads generate production evidence.

The default hydrate_node_prompt return remains str. Runtime signature-filters optional context_budget_bytes/context_plan/mandatory_suffix for older scripted callbacks. String results report empty sources with unknown_legacy_callback; they still receive final byte/token checks. Legacy prefix accounting helpers are retained for historical tooling, not used by production hydration.

## Fixture validation and limits

Focused real-DAG/git tests use scripted workers only. They cover selected small/missing/tiny windows, exact unit event provenance, unsafe-source exclusions, overflow-repack input/byte shrinking, mandatory retry feedback and optional callback signatures. Source lint and strict type checks cover changed source.

Tokens use Core's offline four-character estimate, not a provider tokenizer. Hidden harness overhead is not observed; safety reserve is policy. Revisions remain unknown. Native scan work is not newly bounded. No model/probe calls, installs, memory writes, push or merge occur here.

See the [OpenSpec design](../openspec/changes/bod-303-selected-context-bridge/design.md) and the [first native pack document](BOD-303-WORKER-CONTEXT.md).
