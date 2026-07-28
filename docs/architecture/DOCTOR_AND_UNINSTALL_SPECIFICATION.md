# Verdict Doctor Diagnostic, Auto-Repair (`--fix`), and Reversible Uninstaller Specification

- **Status:** Approved / Specification
- **Date:** 2026-07-28
- **Scope:** Diagnostic health scans, auto-repair capabilities, and safe reversible uninstallation across all AI tool integrations.

## 1. Diagnostic Scanner & Auto-Repair (`verdict doctor`, `verdict doctor --fix`)

| Diagnostic Target | Health Check | Auto-Repair Action (`--fix`) |
|---|---|---|
| **MemoryPlane SQLite Database** | Verifies database exists, is writable, and tables (`memory_records`, FTS) are intact | Re-initializes schema and re-indexes FTS tables if corrupt or missing |
| **MCP Server Registration** | Checks `.mcp.json` for valid `verdict-memory` MCP entry | Injects or updates `.mcp.json` with correct binary and environment variables |
| **Tool Memory Bridges** | Checks `.codex/AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.pi/memory_bridge.json` | Re-injects missing memory bridge headers into tool files |
| **System Permissions & Paths** | Verifies `.verdict/` directory permissions and quarantine rules | Corrects file permissions (`0700` for `.verdict/`) and creates state directories |

---

## 2. Reversible Uninstaller (`verdict uninstall`)

- **Hook Block Stripping**: Safely strips the `# Verdict Unified Memory Bridge` header block from `AGENTS.md`, `CLAUDE.md`, and `.cursorrules` without altering original user instructions.
- **MCP Server Cleanup**: Removes `verdict-memory` entry from `.mcp.json` (leaving other MCP servers untouched).
- **Data Preservation**: Preserves user source files and `.verdict/memory.db` database by default. Data is purged only if `--purge-data` is explicitly specified.

---

## 3. Guarantees
- **Safety First**: Never modifies user code outside of designated tool configuration files.
- **Reversibility**: Uninstallation completely restores pre-setup configuration states.
- **Offline & Atomic**: All diagnostics and repairs run 100% offline without remote network calls.
