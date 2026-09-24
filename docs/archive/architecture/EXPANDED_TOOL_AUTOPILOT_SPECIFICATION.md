# Expanded AI Tool Discovery, MCP Configuration, and Environment Autopilot Specification

- **Status:** Approved / Specification
- **Date:** 2026-07-28
- **Scope:** Universal detection and automated configuration across Codex, Claude Code, Pi, Ruflo/Claude Flow, Hermes, JCode/Cursor, OmniRoute, GitHub CLI, and MCP servers.

## 1. Ecosystem Tool Catalog

| Tool / Environment | Detection Fingerprints | Configuration Targets | Memory & Hook Integration |
|---|---|---|---|
| **Codex** | `~/.codex`, `.codex/` | `.codex/AGENTS.md`, `.codex/config.toml` | Unified Memory Plane, `AGENTS.md` rules |
| **Claude Code** | `~/.claude`, `CLAUDE.md` | `CLAUDE.md`, `.claude/config.json` | Memory plane search & prompt hooks |
| **Pi** | `~/.pi`, `.pi/` | `.pi/memory_bridge.json`, `.pi/config.json` | Subagent memory bridge |
| **Ruflo / Claude Flow** | `~/.claude-flow`, `ruflo`, `ruflo-skills` | `.claude-flow/memory.json`, `.claude-flow/hooks.json` | Swarm ledger & global memory provider |
| **Hermes** | `~/.hermes` | `~/.hermes/verdict_bridge.json` | Context transcript sync |
| **JCode / Cursor / VSCode** | `.vscode/`, `.cursor/`, `.cursorrules`, `~/.jcode` | `.cursorrules`, `.vscode/settings.json` | Project rules & memory context injection |
| **OmniRoute / LLMGate** | `OMNIROUTE_BASE_URL`, `http://127.0.0.1:20128/v1` | `verdict.yaml`, `OMNIROUTE_BASE_URL` env var | Provider failover & strength profile routing |
| **GitHub CLI / Workflows** | `gh` CLI, `.github/workflows/`, `.github/` | `.github/workflows/verdict-audit.yml` | CI/CD verification receipts |
| **MCP Servers** | `.mcp.json`, `~/.mcp.json` | `.mcp.json` | Verdict Memory & Guidance MCP tool exposure |

---

## 2. Automated Autopilot Actions (`verdict memory setup`)

1. **Discovery & Preselection**:
   - Scans system `PATH`, home directory (`~`), workspace root, and environment variables.
   - Preselects 100% of installed/detected tools by default.
2. **MCP Server Integration**:
   - Generates or merges `.mcp.json` configuring the Verdict Memory Plane MCP server (`verdict memory mcp`) and Guidance Control Plane MCP server.
3. **Environment & Hook Configuration**:
   - Sets environment variables: `VERDICT_MEMORY_PLANE_PATH`, `VERDICT_GUIDANCE_ENABLED=1`.
   - Writes memory bridge headers in `AGENTS.md`, `CLAUDE.md`, and `.cursorrules`.
4. **Historical Session Autopilot Sync**:
   - Ingests past session JSONL transcripts from all detected tools into canonical `MemoryPlane`.

---

## 3. Guarantees
- **Non-Destructive Merge**: Existing configurations are preserved; memory bridge blocks are appended deterministically.
- **100% Offline & Deterministic**: Zero network dependencies during detection or setup.
- **Fail-Safe**: Tool absence degrades to an explicit `not_installed` state without throwing errors.
