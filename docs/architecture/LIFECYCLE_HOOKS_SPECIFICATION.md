# Verdict Core Lifecycle Hooks & Context Autopilot Specification

- **Status:** Approved / Specification
- **Date:** 2026-07-28
- **Scope:** Complete lifecycle hook architecture across Codex, Claude Code, Pi, Ruflo, Hermes, and custom AI runtimes.

## 1. Overview
Verdict Core provides a platform-neutral, local-first execution and memory control plane. To ensure complete context capture, policy enforcement, and seamless cross-tool memory recall, Verdict defines an extensible 6-category lifecycle hook matrix.

---

## 2. Complete Hook Matrix

### A. Prompt & Context Hooks
1. `on_prompt` (`pre-prompt`):
   - **Trigger:** Immediately before sending a user prompt to an LLM or agent runtime.
   - **Action:** Queries `MemoryPlane` for relevant records, sanitizes prompt injection attempts, applies `ContextPackCompiler` token budgeting, and prepends context.
2. `on_response` (`post-response`):
   - **Trigger:** Immediately after receiving an LLM/agent response.
   - **Action:** Extracts key decisions, claims, or code artifacts and prepares them for memory indexing.

### B. Task Lifecycle Hooks
3. `on_task_start` (`pre-task`):
   - **Trigger:** When a logical task or goal begins.
   - **Action:** Validates task parameters against `GuidanceControlPlane`, logs task start receipt in `ReceiptStore`, and sets active scope.
4. `on_task_complete` (`post-task`):
   - **Trigger:** When a task reaches a terminal condition (`complete` or `blocked`).
   - **Action:** Writes final task completion receipt, calculates token/time metrics, and updates memory plane.

### C. File & Edit Hooks
5. `on_file_edit_start` (`pre-edit`):
   - **Trigger:** Before writing or patching a file on disk.
   - **Action:** Enforces root allowlist rules, checks quarantine paths (`/tmp`, `/vendor`, `/generated`), creates pre-edit checksum receipt.
6. `on_file_edit_complete` (`post-edit`):
   - **Trigger:** After a file edit is written.
   - **Action:** Computes SHA-256 diff hash, logs file edit provenance, and updates compact code graph summaries.

### D. Command Execution Hooks
7. `on_command_execute` (`pre-command`):
   - **Trigger:** Before executing a shell command or tool call.
   - **Action:** Scans command string for destructive targets (`rm -rf /`, `$HOME`), verifies credential redaction, and evaluates guidance policy (`allow`, `approval_required`, `deny`).
8. `on_command_complete` (`post-command`):
   - **Trigger:** After command process terminates.
   - **Action:** Records exit code, output byte count, execution duration, and logs privacy-safe execution receipt in `ReceiptStore`.

### E. Session Lifecycle Hooks
9. `on_session_start` (`session-start`):
   - **Trigger:** When an AI session initializes.
   - **Action:** Detects host AI tools, preselects memory bridges, and initializes project-scoped `MemoryPlane`.
10. `on_session_end` (`session-end`):
    - **Trigger:** When a session terminates or yields final answer.
    - **Action:** Ingests session transcript JSONL into `MemoryPlane`, exports portable memory manifest, and logs session outcome receipt.
11. `on_session_restore` (`session-restore`):
    - **Trigger:** When resuming from a checkpoint or compaction.
    - **Action:** Restores previous session memory plane state and receipt history without repeating past work.

### F. Verification & Intelligence Hooks
12. `on_verify` (`verify`):
    - **Trigger:** Before code promotion or PR creation.
    - **Action:** Executes automated test, lint, format, typecheck, and security scans; writes verification receipt.
13. `on_error` (`error`):
    - **Trigger:** When an unhandled error, timeout, or failure occurs.
    - **Action:** Captures error metadata, logs outcome receipt, and degrades safely.

---

## 3. Integration with Tools (Codex, Claude Code, Pi, Ruflo)
Each supported tool environment connects to this lifecycle hook matrix via `verdict memory hook <event>` CLI subcommands or Python `MemoryHookController` bindings:
- **Codex:** Configured via `.codex/AGENTS.md` and `.codex/hooks.json`.
- **Claude Code:** Configured via `CLAUDE.md` and `.claude/hooks`.
- **Pi:** Configured via `.pi/config.json`.
- **Ruflo / Claude Flow:** Configured via `.claude-flow/memory.json`.

---

## 4. Guarantees
- **100% Offline & Fast:** Executed in-process or local SQLite with zero network blocking.
- **Privacy & Security:** Credential redaction on all payloads.
- **Fail-Safe Degradation:** Hook failures return explicit `degraded` status without crashing the primary task loop.
