# Verdict Core Lifecycle Hooks & Context Autopilot Specification

- **Status:** Approved / Specification
- **Date:** 2026-07-28
- **Scope:** Complete lifecycle hook architecture across Codex, Claude Code, Pi, and custom AI runtimes.

> Historical note: earlier revisions of this specification also listed
> Ruflo/Claude Flow and swarm coordination as hook targets. That integration
> was removed from Core (BOD-17); see
> [ADR-023](../adr/ADR-023-governed-swarm-supervision.md) (superseded) and
> [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) (current
> orchestration).

## 1. Overview
Verdict Core provides a platform-neutral, local-first execution and memory control plane. To ensure complete context capture, policy enforcement, and seamless cross-tool memory recall, Verdict defines an extensible 6-category lifecycle hook matrix.

---

## 2. Complete Hook Matrix

### A. Prompt & Context Hooks
1. `on_prompt` (`pre-prompt`):
   - **Trigger:** Immediately before sending a user prompt to an LLM or agent runtime.
   - **Action:** Queries `MemoryPlane` for relevant records, applies `ContextPackCompiler` token budgeting, and prepends recalled context to the prompt.
2. `on_response` (`post-response`):
   - **Trigger:** Immediately after receiving an LLM/agent response.
   - **Action:** Logs a context receipt for the response in `ReceiptStore`.

### B. Task Lifecycle Hooks
3. `on_task_start` (`pre-task`):
   - **Trigger:** When a logical task or goal begins.
   - **Action:** Runs the documentation preflight for implementation work and logs a task-start receipt in `ReceiptStore`.
4. `on_task_complete` (`post-task`):
   - **Trigger:** When a task reaches a terminal condition (`complete` or `blocked`).
   - **Action:** Writes the final task-completion receipt in `ReceiptStore`.

### C. File & Edit Hooks
5. `on_file_edit_start` (`pre-edit`):
   - **Trigger:** Before writing or patching a file on disk.
   - **Action:** Rejects quarantined paths (`/tmp/`, `/var/tmp/`), runs the documentation preflight for implementation work, and writes a pre-edit receipt.
6. `on_file_edit_complete` (`post-edit`):
   - **Trigger:** After a file edit is written.
   - **Action:** Records the caller-supplied diff hash and logs file-edit provenance in `ReceiptStore`.

### D. Command Execution Hooks
7. `on_command_execute` (`pre-command`):
   - **Trigger:** Before executing a shell command or tool call.
   - **Action:** Rejects known destructive command patterns (for example `rm -rf /`, `mkfs.`, `shutdown`) and writes a command receipt before execution.
8. `on_command_complete` (`post-command`):
   - **Trigger:** After command process terminates.
   - **Action:** Records exit code and execution duration in a privacy-safe execution receipt in `ReceiptStore`.

### E. Session Lifecycle Hooks
9. `on_session_start` (`session-start`):
   - **Trigger:** When an AI session initializes.
   - **Action:** Writes a session-start receipt scoped to the session and project.
10. `on_session_end` (`session-end`):
    - **Trigger:** When a session terminates or yields final answer.
    - **Action:** Persists the session transcript (when provided) into `MemoryPlane` and logs a session outcome receipt.
11. `on_session_restore` (`session-restore`):
    - **Trigger:** When resuming from a checkpoint or compaction.
    - **Action:** Writes a session-restore receipt; prior memory plane records remain available for recall.

### F. Verification & Intelligence Hooks
12. `on_verify` (`verify`):
    - **Trigger:** Before code promotion or PR creation.
    - **Action:** Records the outcome of a verification check (test, lint, typecheck, or security scan) in a verification receipt.
13. `on_error` (`error`):
    - **Trigger:** When an unhandled error, timeout, or failure occurs.
    - **Action:** Captures error metadata and logs an outcome receipt.

---

## 3. Integration with Tools (Codex, Claude Code, Pi)
Each supported tool environment connects to this lifecycle hook matrix via the `verdict hook` CLI (`recall`, `record`, `configure`, `status`, `claude-gate`) or Python `MemoryHookController` bindings (`verdict/memory_bridge.py`):
- **Codex:** Configured via `.codex/AGENTS.md` and `~/.codex/hooks.json`.
- **Claude Code:** Configured via `CLAUDE.md` and `~/.claude/settings.json` hooks.
- **Pi:** Configured via `.pi/memory_bridge.json`.

---

## 4. Guarantees
- **Local-First & Fast:** Hooks execute in-process against local SQLite with no network calls.
- **Privacy & Security:** Receipt payloads pass through `redact_sensitive_dict` (`verdict/receipt_store.py`) before persistence.
- **Fail-Closed Safety Checks:** Quarantined paths and destructive commands raise explicit errors (`quarantined_path_rejected`, `destructive_command_rejected`) instead of proceeding silently.
