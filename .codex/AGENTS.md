# Verdict Core Control Plane & Codex Configuration

- **Execution Ledger**: All Codex sessions, execution context, and code graphs share one local-first `MemoryPlane` (`/home/nick/.verdict/memory.db`).
- **MCP Integration**: Codex uses `verdict-core` and `verdict-memory` MCP tools (`verdict_route`, `verdict_memory_search`, `verdict_memory_store`, `verdict_qualify`, `verdict_context_pack`, `verdict_health`, `verdict_explain`).
- **Pre-execution**: Before executing task phases, query memory: `verdict memory search '<query>'` or call `verdict_memory_search`.
- **Post-execution**: Persist session outcomes and patterns: `verdict memory put <key> <content>` or call `verdict_memory_store`.


# Verdict Unified Memory Bridge
- All sessions, context, and code graphs share one local-first MemoryPlane.
- Query memory prior to task execution: `verdict memory search '<query>'`.
- Export session records on completion: `verdict memory put <key> <content>`.
