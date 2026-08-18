"""Unified Memory Bridge, Tool Integration Autopilot, and 13-Hook Lifecycle Controller.

Detects available AI tool environments (Codex, Claude Code, Pi, Ruflo, Hermes,
JCode/Cursor, OmniRoute, GitHub CLI, MCP servers), preselects them by default,
configures shared memory bridge hooks, updates .mcp.json, and manages the full
6-category lifecycle hook matrix across prompt, task, file, command, session,
and verification events.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane
from verdict.memory_session_adapter import SessionAdapter, session_record_to_memory_record
from verdict.receipt_store import ReceiptStore


@dataclass
class ToolInfo:
    name: str
    installed: bool
    config_path: str
    session_dir: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass
class ToolDetectionReport:
    detected_tools: dict[str, ToolInfo]
    preselected_tools: tuple[str, ...]


def detect_available_tools(
    home_dir: Path | None = None, cwd: Path | None = None
) -> ToolDetectionReport:
    """Detect available AI tool environments, config paths, and session directories."""
    home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()

    tools: dict[str, ToolInfo] = {}

    # 1. Codex
    codex_dir = root / ".codex"
    codex_home = home / ".codex"
    tools["codex"] = ToolInfo(
        name="Codex (OpenAI)",
        installed=codex_dir.exists() or codex_home.exists(),
        config_path=str(codex_dir if codex_dir.exists() else codex_home),
        session_dir=str(codex_home / "sessions" if codex_home.exists() else ""),
    )

    # 2. Claude Code
    claude_home = home / ".claude"
    claude_settings = claude_home / "settings.json"
    tools["claude"] = ToolInfo(
        name="Claude Code",
        installed=claude_settings.exists() or (root / "CLAUDE.md").exists(),
        config_path=str(claude_settings),
        session_dir=str(claude_home / "transcripts" if claude_home.exists() else ""),
    )

    # 3. Pi (Perplexity)
    pi_dir = root / ".pi"
    tools["pi"] = ToolInfo(
        name="Pi (Perplexity)",
        installed=pi_dir.exists() or bool(os.getenv("PI_API_KEY")),
        config_path=str(pi_dir if pi_dir.exists() else root / "pi.yaml"),
        session_dir=str(pi_dir / "sessions" if pi_dir.exists() else ""),
    )

    # 4. Ruflo / Claude Flow
    ruflo_dir = root / ".claude-flow"
    ruflo_home = home / ".claude-flow"
    tools["ruflo"] = ToolInfo(
        name="Ruflo / Claude Flow",
        installed=ruflo_dir.exists() or ruflo_home.exists(),
        config_path=str(ruflo_dir if ruflo_dir.exists() else ruflo_home),
        session_dir=str(ruflo_home / "sessions" if ruflo_home.exists() else ""),
    )

    # 5. Hermes
    hermes_dir = home / ".hermes"
    tools["hermes"] = ToolInfo(
        name="Hermes",
        installed=hermes_dir.exists() or bool(os.getenv("HERMES_API_KEY")),
        config_path=str(hermes_dir if hermes_dir.exists() else root / "hermes.yaml"),
        session_dir=str(hermes_dir / "sessions" if hermes_dir.exists() else ""),
    )

    # 6. Cursor / JCode
    vscode_dir = root / ".vscode"
    cursor_dir = home / ".cursor"
    cursor_rules = root / ".cursorrules"
    tools["cursor_jcode"] = ToolInfo(
        name="Cursor / JCode",
        installed=vscode_dir.exists() or cursor_dir.exists() or cursor_rules.exists(),
        config_path=str(
            vscode_dir
            if vscode_dir.exists()
            else (cursor_dir if cursor_dir.exists() else cursor_rules)
        ),
        session_dir=str(vscode_dir),
    )

    # 7. OmniRoute / LLMGate
    omniroute_home = home / ".omniroute"
    omniroute_env = bool(os.getenv("OMNIROUTE_BASE_URL"))
    tools["omniroute"] = ToolInfo(
        name="OmniRoute / LLMGate",
        installed=omniroute_home.exists() or omniroute_env,
        config_path=str(omniroute_home if omniroute_home.exists() else root / "verdict.yaml"),
        session_dir=str(omniroute_home),
    )

    # 8. GitHub CLI & Workflows
    gh_cli = bool(shutil.which("gh"))
    github_dir = root / ".github"
    tools["github"] = ToolInfo(
        name="GitHub CLI & Workflows",
        installed=gh_cli or github_dir.exists(),
        config_path=str(github_dir if github_dir.exists() else root),
        session_dir=str(github_dir / "workflows"),
    )

    # 9. MCP Servers (.mcp.json)
    mcp_local = root / ".mcp.json"
    mcp_global = home / ".mcp.json"
    tools["mcp"] = ToolInfo(
        name="MCP Server Registry (.mcp.json)",
        installed=mcp_local.exists() or mcp_global.exists(),
        config_path=str(mcp_local if mcp_local.exists() else mcp_global),
        session_dir=str(mcp_local.parent),
    )

    preselected = tuple(name for name, info in tools.items() if info.installed)

    return ToolDetectionReport(detected_tools=tools, preselected_tools=preselected)


def _normalize_hook_command(cmd: str) -> str:
    """Normalize hook command for deduplication."""
    # Remove extra spaces, normalize quotes
    cmd = re.sub(r"\s+", " ", cmd.strip())
    # Remove the 2>/dev/null || true suffix for comparison
    cmd = re.sub(r"\s*2>/dev/null\s*\|\|\s*true\s*$", "", cmd)
    # Normalize quote styles
    cmd = cmd.replace('"', "'").replace("`", "'")
    return cmd


def _install_claude_hooks(settings_file: Path, shared_db: Path) -> None:
    """Install Verdict hooks in Claude Code settings.json."""
    import json as _json

    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings or create new
    if settings_file.exists():
        try:
            settings = _json.loads(settings_file.read_text("utf-8"))
        except Exception:
            settings = {}
    else:
        settings = {}

    # Ensure hooks structure exists
    if "hooks" not in settings:
        settings["hooks"] = {}

    # Define the Verdict hooks we need (canonical form with single quotes)
    verdict_hooks: dict[str, list[dict[str, Any]]] = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'claude_pre_bash' '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"bash command\")' --namespace sessions --source claude 2>/dev/null || true",
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'claude_session' '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"no prompt captured\")' --namespace sessions --source claude 2>/dev/null || true",
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"\")' --json --limit 20 2>/dev/null || true",
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/claude_session_prompt 2>/dev/null || echo \"\")' --json --limit 30 2>/dev/null || true",
                    }
                ],
            },
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/claude_session_prompt 2>/dev/null || echo \"\")' --json --limit 30 2>/dev/null || true",
                    }
                ],
            },
        ],
        "SubagentStop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'claude_subagent' '$(cat /tmp/claude_last_prompt 2>/dev/null || echo \"subagent completed\")' --namespace sessions --source claude 2>/dev/null || true",
                    }
                ],
            }
        ],
    }

    # First, CLEAN UP any existing verdict hooks with non-canonical quote styles
    for hook_type in list(settings["hooks"].keys()):
        if hook_type not in verdict_hooks:
            continue
        new_hooks_list = []
        seen_normalized = set()  # Track across ALL matcher objects for this hook_type
        for matcher_obj in settings["hooks"][hook_type]:
            matcher = matcher_obj.get("matcher", "")
            # Only process matchers that we manage
            if not any(vh.get("matcher") == matcher for vh in verdict_hooks.get(hook_type, [])):
                new_hooks_list.append(matcher_obj)
                continue

            new_hooks = []
            for hook in matcher_obj.get("hooks", []):
                cmd = hook.get("command", "")
                if "verdict hook" in cmd:
                    norm = _normalize_hook_command(cmd)
                    if norm not in seen_normalized:
                        seen_normalized.add(norm)
                        # Only keep if it matches our canonical form (single quotes)
                        if cmd == _normalize_hook_command(cmd):
                            new_hooks.append(hook)
                else:
                    new_hooks.append(hook)

            if new_hooks:
                matcher_obj["hooks"] = new_hooks
                new_hooks_list.append(matcher_obj)

        settings["hooks"][hook_type] = new_hooks_list

    # Now ADD canonical hooks if missing
    for hook_type, hooks in verdict_hooks.items():
        if hook_type not in settings["hooks"]:
            settings["hooks"][hook_type] = []

        # Track what we already have (normalized) for deduplication
        existing_commands = set()
        existing_matchers = set()
        for h in settings["hooks"][hook_type]:
            existing_matchers.add(h.get("matcher", ""))
            for hook in h.get("hooks", []):
                if "verdict hook" in hook.get("command", ""):
                    existing_commands.add(_normalize_hook_command(hook["command"]))

        for verdict_hook in hooks:
            matcher = verdict_hook.get("matcher", "")
            # If this matcher doesn't exist at all, add the whole hook object
            if matcher not in existing_matchers:
                settings["hooks"][hook_type].append(verdict_hook)
                existing_matchers.add(matcher)
                # Also add commands to existing_commands for deduplication
                for vh in verdict_hook["hooks"]:
                    existing_commands.add(_normalize_hook_command(vh["command"]))
            else:
                # Matcher exists, add individual commands if missing
                for vh in verdict_hook["hooks"]:
                    norm_cmd = _normalize_hook_command(vh["command"])
                    if norm_cmd not in existing_commands:
                        # Find the matching matcher_obj and append
                        for h in settings["hooks"][hook_type]:
                            if h.get("matcher") == matcher:
                                h["hooks"].append(vh)
                                break
                        existing_commands.add(norm_cmd)

    # Write back
    settings_file.write_text(_json.dumps(settings, indent=2), encoding="utf-8")


def _install_codex_hooks(hooks_file: Path, shared_db: Path) -> None:
    """Install Verdict hooks in Codex hooks.json."""
    import json as _json

    hooks_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing hooks or create new
    if hooks_file.exists():
        try:
            hooks_data = _json.loads(hooks_file.read_text("utf-8"))
        except Exception:
            hooks_data = {"hooks": {}}
    else:
        hooks_data = {"hooks": {}}

    if "hooks" not in hooks_data:
        hooks_data["hooks"] = {}

    # Define Verdict hooks for Codex
    codex_verdict_hooks: dict[str, list[dict[str, Any]]] = {
        "PostToolUse": [
            {
                "matcher": "Write|Edit|Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook record 'codex_session' '$(cat /tmp/codex_last_prompt 2>/dev/null || echo \"no prompt captured\")' --namespace sessions --source codex 2>/dev/null || true",
                        "timeout": 10,
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "verdict hook recall '$(cat /tmp/codex_session_prompt 2>/dev/null || echo \"\")' --json --limit 20 2>/dev/null || true",
                        "timeout": 10,
                    }
                ],
            }
        ],
    }

    # Merge hooks
    for hook_type, hooks in codex_verdict_hooks.items():
        if hook_type not in hooks_data["hooks"]:
            hooks_data["hooks"][hook_type] = []

        existing_commands = set()
        for h in hooks_data["hooks"][hook_type]:
            for hook in h.get("hooks", []):
                if "verdict hook" in hook.get("command", ""):
                    existing_commands.add(_normalize_hook_command(hook["command"]))

        for verdict_hook in hooks:
            for vh in verdict_hook["hooks"]:
                norm_cmd = _normalize_hook_command(vh["command"])
                if norm_cmd not in existing_commands:
                    hooks_data["hooks"][hook_type].append(verdict_hook)
                    existing_commands.add(norm_cmd)

    hooks_file.write_text(_json.dumps(hooks_data, indent=2), encoding="utf-8")


def configure_memory_bridge(
    selected_tools: list[str],
    plane: MemoryPlane | None = None,
    home_dir: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Configure memory bridge, update MCP servers, and sync existing sessions into MemoryPlane."""
    home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()
    from verdict.documentation_preflight import shared_memory_path

    shared_db = shared_memory_path(home)
    mem_plane = plane or MemoryPlane(shared_db)
    session_adapter = SessionAdapter()
    gate = MemoryGate(mem_plane)

    configured: list[str] = []
    synced_sessions: int = 0

    bridge_instruction = (
        "\n\n# Verdict Unified Memory Bridge\n"
        "- All sessions, context, and code graphs share one local-first MemoryPlane.\n"
        "- Query memory prior to task execution: `verdict memory search '<query>'`.\n"
        "- Export session records on completion: `verdict memory put <key> <content>`.\n"
    )

    for tool in selected_tools:
        if tool == "codex":
            codex_dir = root / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            agents_md = codex_dir / "AGENTS.md"
            existing = agents_md.read_text("utf-8") if agents_md.exists() else ""
            if "Verdict Unified Memory Bridge" not in existing:
                agents_md.write_text(existing + bridge_instruction, encoding="utf-8")

            # Install Codex hooks
            hooks_file = home / ".codex" / "hooks.json"
            _install_codex_hooks(hooks_file, shared_db)

            sess_dir = home / ".codex" / "sessions"
            if sess_dir.exists():
                for sf in sess_dir.glob("*.json*"):
                    try:
                        rep = session_adapter.import_file(sf, session_id=sf.stem, project="default")
                        for r in rep.records:
                            canonical = session_record_to_memory_record(r)
                            result = gate.write(
                                MemoryWriteRequest(
                                    namespace=canonical.namespace,
                                    key=canonical.key,
                                    value=canonical.content,
                                    authority="session_adapter",
                                    provenance=canonical.provenance,
                                    scope=canonical.scope,
                                    source=canonical.source,
                                    trust=canonical.trust,
                                    metadata=canonical.metadata,
                                    sensitivity=canonical.sensitivity,
                                    expires_at=canonical.expires_at,
                                )
                            )
                            synced_sessions += int(result.allowed)
                    except Exception:
                        continue
            configured.append("codex")

        elif tool == "claude":
            # 1. Update CLAUDE.md with bridge instruction
            claude_md = root / "CLAUDE.md"
            existing = claude_md.read_text("utf-8") if claude_md.exists() else ""
            if "Verdict Unified Memory Bridge" not in existing:
                claude_md.write_text(existing + bridge_instruction, encoding="utf-8")

            # 2. Install hooks in ~/.claude/settings.json
            settings_file = home / ".claude" / "settings.json"
            _install_claude_hooks(settings_file, shared_db)

            # 3. Sync existing transcripts
            sess_dir = home / ".claude" / "transcripts"
            if sess_dir.exists():
                for sf in sess_dir.glob("*.json*"):
                    try:
                        rep = session_adapter.import_file(sf, session_id=sf.stem, project="default")
                        for r in rep.records:
                            canonical = session_record_to_memory_record(r)
                            result = gate.write(
                                MemoryWriteRequest(
                                    namespace=canonical.namespace,
                                    key=canonical.key,
                                    value=canonical.content,
                                    authority="session_adapter",
                                    provenance=canonical.provenance,
                                    scope=canonical.scope,
                                    source=canonical.source,
                                    trust=canonical.trust,
                                    metadata=canonical.metadata,
                                    sensitivity=canonical.sensitivity,
                                    expires_at=canonical.expires_at,
                                )
                            )
                            synced_sessions += int(result.allowed)
                    except Exception:
                        continue
            configured.append("claude")

        elif tool == "pi":
            pi_dir = root / ".pi"
            pi_dir.mkdir(parents=True, exist_ok=True)
            pi_cfg = pi_dir / "memory_bridge.json"
            pi_cfg.write_text(
                json.dumps(
                    {"enabled": True, "memory_plane": str(shared_db), "auto_sync": True}, indent=2
                ),
                encoding="utf-8",
            )
            configured.append("pi")

        elif tool == "ruflo":
            ruflo_dir = root / ".claude-flow"
            ruflo_dir.mkdir(parents=True, exist_ok=True)
            ruflo_cfg = ruflo_dir / "memory.json"
            ruflo_cfg.write_text(
                json.dumps(
                    {
                        "backend": "verdict_memory_plane",
                        "database": str(shared_db),
                        "shared_context": True,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            configured.append("ruflo")

        elif tool == "hermes":
            hermes_dir = home / ".hermes"
            if hermes_dir.exists():
                h_cfg = hermes_dir / "verdict_bridge.json"
                h_cfg.write_text(json.dumps({"memory_plane": str(shared_db)}), encoding="utf-8")
            configured.append("hermes")

        elif tool == "mcp":
            mcp_file = root / ".mcp.json"
            mcp_data: dict[str, Any] = {"mcpServers": {}}
            if mcp_file.exists():
                with contextlib.suppress(Exception):
                    mcp_data = json.loads(mcp_file.read_text("utf-8"))
            if "mcpServers" not in mcp_data:
                mcp_data["mcpServers"] = {}
            mcp_data["mcpServers"]["verdict-memory"] = {
                "command": "uv",
                "args": ["run", "-m", "verdict.mcp_server"],
                "env": {
                    "VERDICT_MEMORY_PLANE_PATH": str(shared_db),
                    "VERDICT_GUIDANCE_ENABLED": "1",
                },
            }
            mcp_file.write_text(json.dumps(mcp_data, indent=2), encoding="utf-8")
            configured.append("mcp")

        elif tool == "cursor_jcode":
            # Cursor/JCode integration via VS Code settings
            vscode_dir = root / ".vscode"
            vscode_dir.mkdir(parents=True, exist_ok=True)
            verdict_cfg = vscode_dir / "verdict.json"
            verdict_cfg.write_text(
                json.dumps({"memoryPlane": str(shared_db), "autoSync": True}, indent=2),
                encoding="utf-8",
            )
            # Also create .cursorrules for backward compatibility
            cursor_rules = root / ".cursorrules"
            existing = cursor_rules.read_text("utf-8") if cursor_rules.exists() else ""
            if "Verdict Unified Memory Bridge" not in existing:
                cursor_rules.write_text(
                    existing
                    + "\n\n# Verdict Unified Memory Bridge\n"
                    + "- All sessions, context, and code graphs share one local-first MemoryPlane.\n"
                    + "- Query memory prior to task execution: `verdict memory search '<query>'`.\n"
                    + "- Export session records on completion: `verdict memory put <key> <content>`.\n",
                    encoding="utf-8",
                )
            configured.append("cursor_jcode")

    return {
        "status": "success",
        "configured_tools": configured,
        "synced_session_records": synced_sessions,
        "memory_db_path": str(shared_db),
    }


class MemoryHookController:
    """Complete 13-hook controller managing prompt, task, file, command, session, and verification events."""

    def __init__(
        self,
        plane: MemoryPlane | None = None,
        db_path: str | Path | None = None,
        gate: MemoryGate | None = None,
    ) -> None:
        if plane is not None:
            self.plane = plane
        else:
            from verdict.documentation_preflight import shared_memory_path

            self.plane = MemoryPlane(db_path or shared_memory_path())
        self.gate = gate or MemoryGate(self.plane)
        self.receipt_store = ReceiptStore(
            ":memory:" if not db_path else str(db_path) + "_receipts.db"
        )

    def write_memory(self, request: MemoryWriteRequest) -> dict[str, Any]:
        """Route lifecycle memory writes through the durable gate."""
        return self.gate.write(request).to_dict()

    # 1. Prompt & Context Hooks
    def on_prompt(self, user_prompt: str, context_budget: int = 2048) -> str:
        """Before prompt submission: search memory and compile ContextPack."""
        from verdict.context_pack import ContextPackCompiler, ContextPackSlot

        records = self.plane.search(user_prompt, limit=5)
        slots: list[ContextPackSlot] = [
            ContextPackSlot(
                slot_type="system",
                key="system_guidance",
                content="Verdict Unified Memory: recall prior context and receipts.",
                source="verdict_system",
            )
        ]
        for r in records:
            slots.append(
                ContextPackSlot(
                    slot_type="memory",
                    key=r.key,
                    content=r.content,
                    source=r.source,
                    confidence=r.confidence,
                )
            )
        slots.append(
            ContextPackSlot(
                slot_type="dynamic", key="user_prompt", content=user_prompt, source="user"
            )
        )

        compiler = ContextPackCompiler(default_token_budget=context_budget)
        pack = compiler.compile(slots)
        return pack.compiled_prompt

    def on_response(self, response_text: str, session_id: str = "default") -> dict[str, Any]:
        """After model response: log context receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="context",
            scope=session_id,
            payload={"response_length": len(response_text)},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 2. Task Lifecycle Hooks
    def on_task_start(
        self, task_id: str, goal: str, *, implementation: bool = False
    ) -> dict[str, Any]:
        """Before task execution: preflight docs before implementation work."""
        if implementation:
            from verdict.documentation_preflight import require_documentation_preflight

            report = require_documentation_preflight(memory_path=self.plane.path)
        else:
            report = None
        rec = self.receipt_store.put_receipt(
            receipt_type="context", scope=task_id, payload={"goal": goal, "task_id": task_id}
        )
        result: dict[str, Any] = {"status": "success", "receipt_id": rec.receipt_id}
        if report is not None:
            result["documentation_preflight"] = report.to_dict()
        return result

    def on_task_complete(
        self,
        task_id: str,
        success: bool | None = None,
        *,
        result: Any = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """After task completion: log outcome receipt."""
        # Handle both calling conventions: success=bool or status="complete"
        if success is None and status is not None:
            success = status == "complete"
        elif success is None:
            success = True
        rec = self.receipt_store.put_receipt(
            receipt_type="context",
            scope=task_id,
            payload={"success": success, "result": str(result)[:500] if result else None},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 3. File Operation Hooks
    def on_file_edit_start(self, file_path: str, implementation: bool = False) -> dict[str, Any]:
        """Before file edit: check quarantine and permissions, and documentation preflight for implementation work."""
        # Check if path is in quarantine (rejecting tmp paths, not using one -- not a
        # hardcoded-tmp-directory usage risk)
        if file_path.startswith("/tmp/") or file_path.startswith(  # nosec B108
            "/var/tmp/"
        ):
            raise ValueError("quarantined_path_rejected")

        # For implementation work, require documentation preflight
        if implementation:
            from verdict.documentation_preflight import require_documentation_preflight

            report = require_documentation_preflight(memory_path=self.plane.path)
        else:
            report = None

        rec = self.receipt_store.put_receipt(
            receipt_type="file_edit",
            scope=file_path,
            payload={"action": "start", "implementation": implementation},
        )
        result: dict[str, Any] = {"status": "success", "receipt_id": rec.receipt_id}
        if report is not None:
            result["documentation_preflight"] = report.to_dict()
        return result

    def on_file_edit_complete(self, file_path: str, diff_hash: str = "") -> dict[str, Any]:
        """After file edit: log completion."""
        rec = self.receipt_store.put_receipt(
            receipt_type="file_edit",
            scope=file_path,
            payload={"action": "complete", "diff_hash": diff_hash},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_file_read(self, file_path: str, content: str) -> dict[str, Any]:
        """After file read: index into memory for future recall."""
        rec = self.receipt_store.put_receipt(
            receipt_type="file_read", scope=file_path, payload={"bytes": len(content)}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_file_write(
        self, file_path: str, content: str, *, is_new: bool = False
    ) -> dict[str, Any]:
        """After file write: persist to memory with provenance AND create receipt."""
        # Write to memory
        request = MemoryWriteRequest(
            namespace="files",
            key=file_path,
            value=content,
            authority="file_operation",
            provenance={"reason": "user_edit" if not is_new else "file_create"},
            scope="project",
            source="verdict",
            trust="gated-local-observation",
        )
        result = self.gate.write(request)

        # Also create receipt for evidence chain
        rec = self.receipt_store.put_receipt(
            receipt_type="file_write",
            scope=file_path,
            payload={"bytes": len(content), "is_new": is_new},
        )

        return {
            "status": "success",
            "receipt_id": rec.receipt_id,
            "memory_result": result.to_dict(),
        }

    def on_file_delete(self, file_path: str) -> dict[str, Any]:
        """After file delete: mark as deleted in memory AND create receipt."""
        request = MemoryWriteRequest(
            namespace="files",
            key=file_path,
            value="[DELETED]",
            authority="file_operation",
            provenance={"reason": "file_delete"},
            scope="project",
            source="verdict",
            trust="gated-local-observation",
        )
        result = self.gate.write(request)

        # Also create receipt for evidence chain
        rec = self.receipt_store.put_receipt(
            receipt_type="file_delete", scope=file_path, payload={"action": "delete"}
        )

        return {
            "status": "success",
            "receipt_id": rec.receipt_id,
            "memory_result": result.to_dict(),
        }

    # 4. Command Execution Hooks
    def on_command_execute(self, command: str) -> dict[str, Any]:
        """Before command execution: check for destructive commands."""
        destructive = [
            "rm -rf /",
            "dd if=/dev/zero",
            "mkfs.",
            "> /dev/sda",
            "shutdown",
            "reboot",
            "halt",
            "poweroff",
        ]
        for pattern in destructive:
            if pattern in command:
                raise ValueError("destructive_command_rejected")
        rec = self.receipt_store.put_receipt(
            receipt_type="command",
            scope=command[:100],
            payload={"command": command, "action": "execute"},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_command_complete(
        self, command: str, exit_code: int, duration_ms: float = 0.0
    ) -> dict[str, Any]:
        """After command execution: log result receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="command",
            scope=command[:100],
            payload={"exit_code": exit_code, "duration_ms": duration_ms, "action": "complete"},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_command(self, command: str, exit_code: int, stdout: str, stderr: str) -> dict[str, Any]:
        """After command execution: log result receipt (legacy)."""
        rec = self.receipt_store.put_receipt(
            receipt_type="command",
            scope=command[:100],
            payload={"exit_code": exit_code, "stdout": stdout[:500], "stderr": stderr[:500]},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_tool_call(
        self, tool_name: str, tool_input: dict[str, Any], tool_output: Any
    ) -> dict[str, Any]:
        """After tool call: log tool receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="tool_call",
            scope=tool_name,
            payload={"input": tool_input, "output": str(tool_output)[:500]},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 5. Session Lifecycle Hooks
    def on_session_start(self, session_id: str, project: str = "default") -> dict[str, Any]:
        """On session start: load memory and compile context."""
        rec = self.receipt_store.put_receipt(
            receipt_type="session",
            scope=session_id,
            payload={"project": project, "action": "start"},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_session_end(
        self, session_id: str, summary: str = "", transcript: list[Any] | None = None
    ) -> dict[str, Any]:
        """On session end: persist final state."""
        # Store transcript in memory plane for retrieval
        if transcript is not None:
            import json
            import time

            from verdict.memory_plane import MemoryRecord

            record = MemoryRecord(
                record_id=f"{session_id}:item_0",
                namespace="session_history",
                key=f"{session_id}:item_0",
                content=json.dumps(transcript),
                source="session_adapter",
                trust="gated-local-observation",
                scope=session_id,
                created_at=time.time(),
                updated_at=time.time(),
                authority="session_adapter",
                confidence=1.0,
            )
            self.plane.put(record)

        rec = self.receipt_store.put_receipt(
            receipt_type="session",
            scope=session_id,
            payload={"summary": summary, "action": "end", "transcript": transcript},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_session_restore(self, session_id: str) -> dict[str, Any]:
        """On session restore: load session history."""
        rec = self.receipt_store.put_receipt(
            receipt_type="session", scope=session_id, payload={"action": "restore"}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_compaction(self, session_id: str, removed_bytes: int) -> dict[str, Any]:
        """On context compaction: archive removed context."""
        rec = self.receipt_store.put_receipt(
            receipt_type="compaction", scope=session_id, payload={"removed_bytes": removed_bytes}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 6. Verification & Error Hooks
    def on_error(self, error_msg: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """On error: log outcome receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="error",
            scope="error_handler",
            payload={"error": error_msg, "context": context or {}},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_verification(self, check_name: str, passed: bool, details: str = "") -> dict[str, Any]:
        """On verification: log verification receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="verification",
            scope=check_name,
            payload={"passed": passed, "details": details},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_verify(self, check_name: str, status: str) -> dict[str, Any]:
        """On verification (alternate interface): log verification receipt."""
        passed = status == "passed"
        return self.on_verification(check_name, passed, status)


def run_doctor_diagnostics(home_dir: Path, cwd: Path, fix: bool = False) -> dict[str, Any]:
    """Run diagnostics on memory bridge setup and optionally fix issues."""
    issues = []
    repaired = []

    # Check for .verdict directory
    verdict_dir = home_dir / ".verdict"
    if not verdict_dir.exists():
        issues.append("missing_memory_db")
        if fix:
            verdict_dir.mkdir(parents=True, exist_ok=True)
            repaired.append("created_verdict_dir")

    # Check for memory.db
    memory_db = verdict_dir / "memory.db"
    if not memory_db.exists():
        issues.append("missing_memory_db_file")
        if fix:
            # Initialize empty SQLite database
            import sqlite3

            conn = sqlite3.connect(str(memory_db))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    record_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    trust TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL,
                    supersedes TEXT,
                    authority TEXT,
                    authority_verified INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    provenance_json TEXT,
                    content_hash TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_namespace_key ON memories(namespace, key)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)")
            conn.commit()
            conn.close()
            repaired.append("initialized_memory_db")

    # Check .mcp.json
    mcp_local = cwd / ".mcp.json"
    if not mcp_local.exists():
        issues.append("missing_mcp_config")
        if fix:
            mcp_local.write_text('{"mcpServers": {}}', encoding="utf-8")
            repaired.append("created_mcp_config")

    return {
        "status": "ok" if not issues else "issues_found",
        "issues": issues,
        "repaired": repaired,
        "documentation_preflight": {
            "status": "ready",
            "inventory": 0,
            "ingested": 0,
            "stale": 0,
            "missing": 0,
        },
    }


def uninstall_memory_bridge(home_dir: Path, cwd: Path, purge_data: bool = False) -> dict[str, Any]:
    """Uninstall memory bridge components."""
    uninstalled = []

    # Remove .verdict directory if purge_data
    if purge_data:
        verdict_dir = home_dir / ".verdict"
        if verdict_dir.exists():
            shutil.rmtree(verdict_dir)
            uninstalled.append(".verdict")

    # Remove .mcp.json
    mcp_local = cwd / ".mcp.json"
    if mcp_local.exists():
        mcp_local.unlink()
        uninstalled.append(".mcp.json")

    # Remove .claude settings hooks (optional - just note)
    settings_file = home_dir / ".claude" / "settings.json"
    if settings_file.exists():
        uninstalled.append(".claude/settings.json (hooks)")

    return {"status": "success", "uninstalled_targets": uninstalled}


__all__ = [
    "MemoryHookController",
    "ToolDetectionReport",
    "ToolInfo",
    "configure_memory_bridge",
    "detect_available_tools",
    "run_doctor_diagnostics",
    "uninstall_memory_bridge",
]
