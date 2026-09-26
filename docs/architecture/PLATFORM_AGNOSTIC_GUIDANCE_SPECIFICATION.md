# Platform-Agnostic Guidance & Rules Enforcement Specification

> Live decision record: [ADR-003 — Platform-Neutral Guidance Boundary](../adr/ADR-003-platform-neutral-guidance-boundary.md)
> (status: proposed). Guidance is optional and disabled by default
> (`VERDICT_GUIDANCE_ENABLED`).
>
> Historical note: earlier revisions of this specification listed Ruflo /
> Claude Flow as a supported guidance platform. That integration was removed
> from Core; see [ADR-023](../adr/ADR-023-governed-swarm-supervision.md)
> (superseded) and [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md)
> (current orchestration).

## Overview

The Verdict **Platform-Agnostic Guidance Boundary** (`verdict/guidance.py`) provides deterministic, harness-independent evaluation of project rules, safety bounds, and architectural constraints.

It is host-neutral by design (ADR-003): the same guidance file works regardless of which AI developer tool drives the session, including:
1. Codex (`.codex/AGENTS.md`)
2. Claude Code (`CLAUDE.md`)
3. Pi (`.pi/memory_bridge.json`)
4. Cursor / VSCode (`.cursorrules`, `.vscode/`)
5. GitHub CLI & Workflows
6. Model Context Protocol (MCP) Servers (`.mcp.json`)

---

## Guidance Rules Taxonomy & Format

Guidance rules are loaded from the project `GUIDANCE.md` file (path configurable via `VERDICT_GUIDANCE_PATH`; an optional local overlay via `VERDICT_GUIDANCE_LOCAL_PATH`) using explicit Markdown bullet formats:

```markdown
- [deny] Never commit hardcoded credentials or API keys.
- [approval] Modifications to database schema require approval.
- [allow] Read-only repository inspection and diagnostics.
```

If no explicit marker is provided, the rule carries no decision on its own. Evaluation (`GuidanceControlPlane.evaluate`) returns `deny` when any matched rule denies, `approval_required` when a matched rule requires approval or the task is protected work, and `allow` otherwise. The result never grants execution authority (`"authorization": "unchanged"`).

---

## Lifecycle Hook Rules Enforcement Matrix

Rules evaluation composes with the Verdict lifecycle hook matrix (`MemoryHookController` in `verdict/memory_bridge.py`; see `LIFECYCLE_HOOKS_SPECIFICATION.md`):

| Lifecycle Hook | Rule Enforcement Action | Safety & Verification Output |
|---|---|---|
| `on_prompt` / `pre-task` | Evaluates prompt intent against guidance rules. | Emits `allow`, `approval_required`, or `deny` decision. |
| `pre-edit` | Validates file target path against boundary rules. | Prevents path traversal or edits outside permitted root. |
| `post-edit` | Audits modified lines against quality and security rules. | Flags hardcoded secrets or prohibited pattern additions. |
| `pre-command` | Checks shell execution commands for destructive flags. | Blocks dangerous commands (`rm -rf /`, `git reset --hard`). |
| `post-command` | Inspects command exit status and output. | Logs verification receipt in `ReceiptStore`. |
| `session_start` | Injects active guidance policy into agent memory plane. | Establishes session memory bridge and context bounds. |
| `session_end` | Summarizes session transcript and exports receipts. | Consolidates key session patterns for next session recall. |

---

## Doctor (`verdict doctor --fix`) & Reversible Uninstaller (`verdict uninstall`)

### 1. Doctor Auto-Repair
Scans guidance file availability, tool configuration headers, `MemoryPlane` database integrity, and `.mcp.json` definitions. When `--fix` is passed, missing headers are injected and corrupted tables are re-initialized. See `DOCTOR_AND_UNINSTALL_SPECIFICATION.md`.

### 2. Reversible Uninstaller
`verdict uninstall` cleanly strips the `# Verdict Unified Memory Bridge` blocks from `AGENTS.md`, `CLAUDE.md`, and `.cursorrules` without modifying original user code. User memory data is preserved unless `--purge-data` is specified.
