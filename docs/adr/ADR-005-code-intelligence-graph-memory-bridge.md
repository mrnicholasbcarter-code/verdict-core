# ADR-005: Code Intelligence Graph and Memory Bridge Design
- **Status:** Approved
- **Date:** 2026-07-28
- **Deciders:** Verdict Core maintainers
- **Related:** [#121](https://github.com/mrnicholasbcarter-code/verdict-core/issues/121), [#141](https://github.com/mrnicholasbcarter-code/verdict-core/issues/141), [#142](https://github.com/mrnicholasbcarter-code/verdict-core/issues/142), [#143](https://github.com/mrnicholasbcarter-code/verdict-core/issues/143)

## Context
Code intelligence and graph analysis (AST parsing, call graphs, betweenness centrality bridges, architectural hotspots) are essential for cross-session reasoning across AI tools (Codex, Claude Code, Pi, Ruflo, Hermes). Storing massive raw external graph databases directly inside local SQLite tables is inefficient and duplicative. A clean architecture must separate graph generation/indexing from canonical memory representation.

## Decision
1. **Lightweight Symbol & Graph Summary Ingestion**:
   - `CodeGraphAdapter` extracts high-value, compact graph summary records (key class/function definitions, architectural bridge nodes, hotspots, and file relationships) into canonical `MemoryRecord` shapes inside `MemoryPlane`.
   - Full heavy graph traversals remain on-demand using in-memory or vector-indexed graph tools (e.g. RuVector / Code Review Graph) without bloating the SQLite memory store.

2. **Unified Tool Memory Bridge & Autopilot**:
   - `verdict memory setup` detects all available AI tool environments (Codex, Claude Code, Pi, Ruflo, Hermes) on the host and preselects installed tools by default.
   - Configures `AGENTS.md`, `CLAUDE.md`, and tool-specific memory configurations to ensure shared cross-tool session context, document recall, and code graph awareness.

3. **Lifecycle Hooks (`on_prompt`, `on_session_end`, `on_task_start`, `on_tool_call`, `on_error`)**:
   - `on_prompt`: Queries `MemoryPlane`, compiles an injection-safe `ContextPack`, and prepends relevant historical context before prompt submission.
   - `on_session_end`: Automatically ingests transcripts into `MemoryPlane` and records privacy-safe execution/outcome receipts into `ReceiptStore`.

## Consequences
- Zero duplication of heavy raw graph databases in SQLite.
- Fast, predictable local memory recall across all AI tools.
- Complete offline independence from third-party services and hosted models.
