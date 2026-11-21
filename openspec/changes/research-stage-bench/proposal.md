# Research-stage bench (RS1)

**Ticket:** #1246 (story RS1). **Operator decision 4:** research can be delegated to cheaper models and synthesised into context packs, so implementation models do not spend usage looking things up.
**Depends on:** FU1 (#1224) harness factored for reuse (task set format, arm runner, baseline comparison); M0 baseline (#1213). S3b is merged, so free routes are admitted for the researcher role.

## Why

Today a worker node gets a retrieval-only pack (`verdict/orchestration/worker_context.py::build_worker_context`). Whether a research stage makes better packs is unknown:

- In the research stage, free routes gather cited findings and a frontier model reads the cited sources and writes the pack.
- The negative-results ledger says three things matter:
  - cheap researchers mis-cite (N43);
  - a bigger pack can look better only because it is bigger (N25);
  - free routes do well only at bounded read-only work (N47).

This change adds a bench that answers the question with downstream verified passes. It changes no runtime behaviour.

## What changes

- A bench command, `verdict bench research-pack`, runs three arms on the same tasks with the same worker route and budget:
  - **A**: today's pack.
  - **B**: free researchers plus frontier synthesis.
  - **C**: arm A padded to arm B's pack size.
- Each arm is graded by whether the node's own verification command passes on the first attempt.
- The task set comes from real closed stories, with the selection criteria recorded next to it.
- Researchers run on `FREE` routes only, read-only, with bounded calls and tokens. Every finding cites a primary source.
- The synthesis reads every cited source, drops findings whose citation does not check, and counts them as citation errors.
- The bench refuses to run without a stop-rule file written before the run. The report embeds the rule's hash.
- The report gives per arm:
  - first-attempt pass rate;
  - frontier tokens per accepted node, including research and synthesis;
  - free tokens;
  - citation errors caught;
  - the repeated-research share.
- Reports and raw receipts go under `docs/proof/research-bench/`, indexed in `docs/proof/EVIDENCE_INDEX.md`.

## Not in scope

- No research stage in `verdict orchestrate`, no pack change and no parallel writers (N41).
- Nothing enters the runtime before about 20 tasks and an operator decision.
