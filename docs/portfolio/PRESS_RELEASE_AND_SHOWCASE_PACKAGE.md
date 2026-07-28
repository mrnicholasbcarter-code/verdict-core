# Verdict Core Launch Package & Press Release

## Press Release / Announcement

### FOR IMMEDIATE RELEASE

**Verdict & RuVector Ship Unified Local-First Memory and Evidence Control Plane for Autonomous AI Agents**

*New open-source framework eliminates cloud vector database limits, unifies context across 9 AI agent tools, and brings deterministic guidance enforcement to multi-agent swarms.*

**SAN FRANCISCO, CA — July 28, 2026** — Today, the Verdict Core team announced the release of **Verdict v0**, a local-first evidence control plane and unified memory system (`MemoryPlane`) designed for autonomous AI coding agents and swarms.

As developer teams increasingly rely on AI agent tools such as OpenAI Codex, Claude Code, Pi, Ruflo, Cursor, and Hermes, context isolation and cloud embedding usage limits have created major bottlenecks. AI agents frequently lose context across sessions, overwrite each other's work, or burn expensive API calls re-indexing codebase files.

Verdict solves this by providing a unified, local-first memory plane (`~/.verdict/memory.db`) powered by a native SQLite + HNSW vector index (`ruvector.db`). Verdict operates 100% offline, allowing AI agents across different harnesses to share session memory, document context, and AST code intelligence graphs in real time under 5 milliseconds.

"Developers shouldn't have to lock themselves into a single AI CLI or pay continuous cloud subscription fees just to keep their AI agents from forgetting what happened in the last session," said Nicholas Carter, Lead Architect of Verdict Core. "Verdict gives AI agents a shared, permanent brain that runs locally on your machine with zero setup friction."

### Key Features of Verdict Core

- **Unified Local-First MemoryPlane**: Zero network calls, zero OpenViking dependencies, instant cross-tool session recall.
- **9-Ecosystem Autopilot**: Automatically detects installed AI agent tools (Codex, Claude Code, Pi, Ruflo, Hermes, Cursor, VSCode, OmniRoute, MCP) and configures memory bridges.
- **AST Code Intelligence Graph**: Parses Python codebases into entity nodes and edges, calculating architectural bridge nodes and degree hub hotspots without heavy external graph databases.
- **Platform-Agnostic Guidance Boundary**: Enforces project safety rules and quality invariants across 6-category lifecycle hooks (`on_prompt`, `pre_edit`, `post_command`, `session_end`).
- **Self-Healing Doctor & Uninstaller**: `verdict doctor --fix` automatically repairs broken MCP definitions or missing memory headers, while `verdict uninstall` provides clean, reversible removal.

Verdict Core is open-source and available immediately on GitHub at `https://github.com/mrnicholasbcarter-code/verdict-core` and as a standalone memory engine at `https://github.com/mrnicholasbcarter-code/verdict-core-memory`.

---

## Credibility-First 5-Minute Demo Walkthrough

1. **One-Command Setup**: `verdict memory setup --autopilot` automatically scans the host system, detects installed AI tools (Codex, Claude Code, Cursor, MCP), and links the shared Verdict memory bridge.
2. **Context Memory Recall**: Demonstrates Codex recording a task session in `.codex/` and Claude Code instantly recalling that session context in `CLAUDE.md` via `MemoryPlane`.
3. **AST Code Graph Search**: Demonstrates searching for architectural chokepoints (`bridge_nodes`) and calculating blast radius (`get_impact_radius`) across modified modules.
4. **Rules Enforcement**: Demonstrates `verdict` blocking a dangerous shell command (`rm -rf`) via `pre-command` lifecycle hook.
5. **Doctor & Reversible Uninstall**: Runs `verdict doctor --fix` to verify system health, followed by `verdict uninstall` showing clean header removal without code disruption.
