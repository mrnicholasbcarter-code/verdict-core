# Worker context packs

Each worker prompt carries an advisory context pack built for its node by
`verdict/orchestration/worker_context.py`. It is quoted evidence for the worker,
never route authority. The production path is `run.prompt_for` ->
`planner.hydrate_node_prompt(structured=True)` -> `WorkerPrompt` -> `DagRuntime`.

## What goes in

- Required contracts, tests and design excerpts first, then target definitions,
  callers/references, test-impact pointers and relevant project docs.
- Target symbols come from the node objective/acceptance or local source.
  Retrieval is for that node only: no sibling node contracts, no transcripts.
- Citations carry excerpt digest, exact span when verified, observation time,
  trust, authority and an explicit unknown revision.

## Budgets

- The runtime builds a ContextPlan from the selected route. Context is charged
  against the route's observed window minus output and safety reserves, and the
  shrinking byte allowance. A missing window stays unknown and uses a local
  target, never an invented observed window.
- Mandatory goal, objective, acceptance, ownership, verification and retry
  feedback are counted first and never truncated; whole evidence units shrink.
  A prompt that cannot fit emits `context_input_rejected` and blocks locally,
  with no executor launch and no provider cooldown.
- Tokens use the offline four-character estimate, not a provider tokenizer.

## Bounded discovery

Native code providers walk the repository with skipped directories pruned and
read at most 2 MiB per file. They parse only files whose bytes name a target
symbol. A scan that exceeds `MAX_SCAN_FILES` Python files reports the gap
`traversal_bounded` rather than failing silently.

## Safety

Sensitive content is rejected before slicing: credential shapes shared with
`verdict.security` (`CREDENTIAL_SHAPES`), dotfiles, transcripts and symlinks
that leave the worktree. A provider failure becomes a named gap; a query failure
falls back to safe required excerpts. No durable memory is written.

## Evidence

`hydrate` events record the actual selected pack units, unit IDs, excerpt
digests, spans, omissions, pack/route/budget facts and measured bytes. A plain
string `prompt_for` callback (tests, legacy tooling) reports empty sources with
`unknown_legacy_callback` and counts as truncated only when its own text says so.

See the OpenSpec changes
[worker context packs](../../openspec/changes/bod-303-worker-context-packs/proposal.md)
and [selected context bridge](../../openspec/changes/bod-303-selected-context-bridge/design.md),
[ADR-022](../adr/ADR-022-context-provider-conformance.md) and
[ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
