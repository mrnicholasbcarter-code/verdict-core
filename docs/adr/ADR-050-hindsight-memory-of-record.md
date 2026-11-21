# ADR-050: Hindsight is the memory of record

**Status:** Accepted (operator, 2026-11-21: "Fully decommission [ai-memory]";
"transfer everything from ai-memory to hindsight first";
"Self-host it here [Recommended]")
**Date:** 2026-11-21
**Deciders:** Operator
**Supersedes:** decision log D1 ("Memory = ai-memory", Linear BOD-342)
**Relates to:** issues #1253 (MEM1, this change), #1254 (MEM2, Hermes/harness removal);
decision log D33

## Context

Verdict and its coding-agent harnesses (Hermes, Prime Agent) wrote durable memory and
performed per-turn recall against an `ai-memory` server (a self-hosted Rust process,
`http://127.0.0.1:49374`). Two related problems emerged:

1. **Doctor rot.** `verdict memory ref-docs sync` (run hourly by `verdict-ref-docs.timer`)
   copied upstream OmniRoute docs into an `ai-memory` project. `verdict doctor` checked
   whether the timer was active and the synced tag matched the installed tag. After the
   OmniRoute 3.8.54 upgrade the 15:00Z daily acceptance run went red because the timer
   had not yet synced the new tag. A red doctor blocks the Gate 1 count and blocks CI
   green for acceptance.

2. **Recall quality.** N51 (2026-11-11) measured operator recall at hit@5 0.70 against
   the 0.90 bar; the root cause was the nomic-embed-text vector stream degrading in
   ai-memory 3.4.5. Mode b (embeddings off, reranker on) recovered to 0.825 but still
   missed the 0.90 bar.

On 2026-11-21 the operator replaced ai-memory with Hindsight as the memory of record.
Hindsight runs self-hosted on this host (Docker, API at `http://127.0.0.1:8888`, one
bank `shared`) and is wired into the coding agents by the
`@vectorize-io/hindsight-coding-agents` plugin. Agents use MCP tools
`hindsight_search_pages`, `hindsight_read_page`, `hindsight_reflect`,
`hindsight_ingest_document`.

The operator's exact answers on 2026-11-21:

* "How far should I take ai-memory out?" → **Fully decommission (Recommended)**
* Next: **transfer everything from ai-memory to hindsight first**
* Memory backend: **Self-host it here (Recommended)**

## Decision

Hindsight replaces ai-memory as the memory of record for Verdict and its coding agents.
The decommission is split into two stories:

* **MEM1 (#1253, this branch):** Remove Verdict's ai-memory reference-docs sync:
  `verdict/ref_docs.py`, `verdict/ref_docs_service.py`, the three `memory ref-docs`
  CLI actions and their parser entries, the doctor rows for the ref-docs timer, the
  `host.ref-docs-timer` setup provider, and all tests covering only removed code.
  `verdict doctor` must no longer depend on ai-memory or the ref-docs timer.

* **MEM2 (#1254, next branch):** Remove the remaining ai-memory integrations: the
  Hermes memory harness, the Hermes plugin, the `AI_MEMORY_AUTH_TOKEN` credential,
  and the setup-wizard offer.

## Consequences

**Positive:**
- `verdict doctor` and the hourly acceptance run no longer depend on ai-memory. The
  timer drift that caused the 15:00Z acceptance failure is gone.
- Agents use Hindsight's reranker-first recall, which achieves higher hit@5 than the
  ai-memory vector stream.
- Fewer moving parts; the ai-memory server can be stopped after MEM2 lands.

**Neutral:**
- `openspec/changes/ref-docs-in-memory/` remains unarchived until MEM2 closes out the
  full decommission. Its tasks reference ai-memory infrastructure that MEM2 removes.

**Negative / accepted risk:**
- The ref-docs pages in ai-memory are not migrated; they were reference material for
  agents, and Hindsight's `hindsight_search_pages` replaces that path.
- MEM2 is a separate PR. Until it lands, the Hermes harness still writes to ai-memory.

## Code and tests this change modifies

- Deleted: `verdict/ref_docs.py`, `verdict/ref_docs_service.py`,
  `tests/test_ref_docs.py`, `tests/test_ref_docs_integration.py`,
  `tests/test_ref_docs_service.py`, `tests/fixtures/ref_docs/`.
- Changed: `verdict/doctor_diagnostics.py`, `verdict/setup_host.py`,
  `verdict/actions/extra.py`, `verdict/actions/registry.py`,
  `verdict/commands/parsers_runtime.py`, `verdict/cli.py`,
  `tests/test_doctor_host_prerequisites.py`, `tests/test_doctor_installed_state.py`,
  `tests/test_doctor_shared_memory.py`, `tests/test_cli_inprocess.py`,
  `tests/test_first_run_messages.py`, `tests/test_setup_host.py`,
  `tests/conftest.py`, `docs/CLI_REFERENCE.md`, `docs/guides/reference-docs.md`,
  `AGENTS.md`, `ETHOS.md`.
- New: this ADR, `openspec/changes/remove-ref-docs-sync/`, D33 in `docs/decisions/LOG.md`.
