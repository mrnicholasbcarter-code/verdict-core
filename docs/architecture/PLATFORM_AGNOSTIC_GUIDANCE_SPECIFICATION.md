# Platform-Agnostic Guidance & Rules Enforcement Specification

## Overview

The Verdict **Platform-Agnostic Guidance Boundary** (`verdict/guidance.py`) provides deterministic, harness-independent enforcement of project rules, safety bounds, and architectural constraints.

It operates seamlessly across all major AI developer tools:
1. Codex (`.codex/AGENTS.md`)
2. Claude Code (`CLAUDE.md`)
3. Pi (`.pi/memory_bridge.json`)
4. Ruflo / Claude Flow (`.claude-flow/memory.json`)
5. Hermes (`~/.hermes/verdict_bridge.json`)
6. JCode / Cursor / VSCode (`.cursorrules`, `.vscode/`)
7. OmniRoute / LLMGate
8. GitHub CLI & Workflows
9. Model Context Protocol (MCP) Servers (`.mcp.json`)

---

## Guidance Rules Taxonomy & Format

Guidance rules are loaded from project `GUIDANCE.md` or `.codex/AGENTS.md` files using explicit Markdown bullet formats:

```markdown
- [deny] Never commit hardcoded credentials or API keys.
- [approval] Modifications to database schema require approval.
- [allow] Read-only repository inspection and diagnostics.
```

If no explicit marker is provided, rules default to evaluation based on action risk and sensitivity.

---

## Lifecycle Hook Rules Enforcement Matrix

Rules enforcement is hooked directly into the Verdict 6-category lifecycle hook matrix:

| Lifecycle Hook | Rule Enforcement Action | Safety & Verification Output |
|---|---|---|
| `on_prompt` / `pre-task` | Evaluates prompt intent against guidance rules. | Emits `allow`, `approval_required`, or `deny` decision receipt. |
| `pre-edit` | Validates file target path against boundary rules. | Prevents path traversal or edits outside permitted root. |
| `post-edit` | Audits modified lines against quality and security rules. | Flags hardcoded secrets or prohibited pattern additions. |
| `pre-command` | Checks shell execution commands for destructive flags. | Blocks dangerous commands (`rm -rf /`, `git reset --hard`). |
| `post-command` | Inspects command exit status and output. | Logs verification receipt in `ReceiptStore`. |
| `session_start` | Injects active guidance policy into agent memory plane. | Establishes session memory bridge and context bounds. |
| `session_end` | Summarizes session transcript and exports receipts. | Consolidates key session patterns for next session recall. |

---

## Doctor (`verdict doctor --fix`) & Reversible Uninstaller (`verdict uninstall`)

### 1. Doctor Auto-Repair
Scans guidance file availability, tool configuration headers, `MemoryPlane` database integrity, and `.mcp.json` definitions. When `--fix` is passed, missing headers are injected and corrupted tables are re-initialized.

### 2. Reversible Uninstaller
`verdict uninstall` cleanly strips the `# Verdict Unified Memory Bridge` blocks from `AGENTS.md`, `CLAUDE.md`, and `.cursorrules` without modifying original user code. User memory data is preserved unless `--purge-data` is specified.
