# BOD-303: first native worker context slice

The queen's existing production worker hydrator now builds advisory context through
`verdict/orchestration/worker_context.py`. It reuses Core query planning, native
retrieval, capability registry defaults and the ContextPack compiler.

## Behavior

- Prefer required contracts, tests and design excerpts. Add target definitions,
  callers/references, test-impact pointers and relevant project docs.
- Derive target symbols from the node objective/acceptance or local source.
  Retrieve only for that node, not sibling node contracts or raw transcripts.
- Render source citations with excerpt digest, exact span when verified,
  observation time, trust, authority and explicit unknown revision.
- Count the entire context projection against bytes and estimated tokens.
  Default context is capped at 4096 estimated tokens with
  `budget_source=conservative_unknown_candidate`. This is not a selected-model
  window claim. The existing overflow retry still narrows the byte allowance.
- `hydrate_node_prompt(..., context_plan=plan)` additionally respects supplied
  candidate window/output/tool reserves and counts mandatory prompt tokens.
  Mandatory ownership/verification instructions are never truncated.
- A provider failure becomes a named gap. A query failure falls back to safe
  required excerpts. Exception payloads are not rendered.
- Reject sensitive content before slicing. Exclude dotfiles, transcripts and
  outside-worktree symlink payloads. Open or write no durable memory. Future
  memory writes must use MemoryGate; prompts/transcripts are not memory facts.

## Evidence and limits

The focused tests are fixtures, including a scripted DagRuntime worker dispatch.
They prove the production prompt path is wired and bounded. They do not prove
live execution quality, savings or self-hosted COMPLETE. Invalid local configured
keys currently block that live proof.

The runtime does not yet forward a selected model ContextPlan automatically.
Its `hydrate` source events still use legacy file-prefix accounting. In this
increment, use prompt citations and the returned WorkerContext pack/omissions for
actual excerpt provenance. Coordinator-owned runtime/run wiring is a second
increment. Native discovery scans the repository; emitted units are bounded, but
scan-time and every internal discovery read are not newly bounded here.

See the contract in
[the OpenSpec change](../openspec/changes/bod-303-worker-context-packs/proposal.md).
This slice implements [ADR-0001](adr/0001-verdict-control-plane-invariants.md)
and [ADR-022](adr/ADR-022-context-provider-conformance.md), without changing
[ADR-036](adr/ADR-036-goal-to-receipt-orchestration.md) route authority.

## Focused validation (fixture evidence)

- `python -m pytest -q tests/test_orch_worker_context.py tests/test_orch_planner.py`: 95 passed.
- Scoped ruff check/format and strict mypy: passed.
- Verdict change-v1 contract validator: passed.
- Doc links: passed. Reachability: no new findings (one unrelated stale baseline warning).

These checks use the project's existing environment. No full suite, provider
probe, live model call or self-hosted completion proof was run.
