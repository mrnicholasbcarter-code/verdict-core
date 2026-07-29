"""Unified Memory Bridge, Tool Integration Autopilot, and 13-Hook Lifecycle Controller.

Detects available AI tool environments (Codex, Claude Code, Pi, Ruflo, Hermes,
JCode/Cursor, OmniRoute, GitHub CLI, MCP servers), preselects them by default,
configures shared memory bridge hooks, updates .mcp.json, and manages the full
6-category lifecycle hook matrix across prompt, task, file, command, session,
and verification events.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class ToolDetectionReport:
    """Report of detected AI tool environments on the host."""

    detected_tools: dict[str, dict[str, Any]]
    preselected_tools: tuple[str, ...]


def detect_available_tools(
    home_dir: Path | None = None, cwd: Path | None = None
) -> ToolDetectionReport:
    """Detect available AI agent tools across all 9 ecosystem categories and preselect installed tools."""
    home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()

    tools: dict[str, dict[str, Any]] = {}

    # 1. Codex
    codex_home = home / ".codex"
    codex_local = root / ".codex"
    tools["codex"] = {
        "name": "Codex",
        "installed": codex_home.exists() or codex_local.exists(),
        "config_path": str(codex_local if codex_local.exists() else codex_home),
        "session_dir": str(codex_home / "sessions"),
    }

    # 2. Claude Code
    claude_home = home / ".claude"
    claude_md = root / "CLAUDE.md"
    tools["claude"] = {
        "name": "Claude Code",
        "installed": claude_home.exists() or claude_md.exists(),
        "config_path": str(claude_home if claude_home.exists() else root),
        "session_dir": str(claude_home / "transcripts"),
    }

    # 3. Pi
    pi_home = home / ".pi"
    pi_local = root / ".pi"
    tools["pi"] = {
        "name": "Pi",
        "installed": pi_home.exists() or pi_local.exists(),
        "config_path": str(pi_local if pi_local.exists() else pi_home),
        "session_dir": str(pi_home / "sessions"),
    }

    # 4. Ruflo / Claude Flow
    ruflo_home = home / ".claude-flow"
    ruflo_local = root / ".claude-flow"
    tools["ruflo"] = {
        "name": "Ruflo / Claude Flow",
        "installed": ruflo_home.exists() or ruflo_local.exists() or (root / "ruflo").exists(),
        "config_path": str(ruflo_local if ruflo_local.exists() else ruflo_home),
        "session_dir": str(ruflo_home / "memory"),
    }

    # 5. Hermes
    hermes_home = home / ".hermes"
    tools["hermes"] = {
        "name": "Hermes",
        "installed": hermes_home.exists(),
        "config_path": str(hermes_home),
        "session_dir": str(hermes_home / "history"),
    }

    # 6. JCode / Cursor / VSCode
    cursor_file = root / ".cursorrules"
    vscode_dir = root / ".vscode"
    jcode_home = home / ".jcode"
    tools["cursor_jcode"] = {
        "name": "JCode / Cursor / VSCode",
        "installed": cursor_file.exists() or vscode_dir.exists() or jcode_home.exists(),
        "config_path": str(cursor_file if cursor_file.exists() else vscode_dir),
        "session_dir": str(vscode_dir),
    }

    # 7. OmniRoute / LLMGate
    omniroute_home = home / ".omniroute"
    omniroute_env = bool(os.getenv("OMNIROUTE_BASE_URL"))
    tools["omniroute"] = {
        "name": "OmniRoute / LLMGate",
        "installed": omniroute_home.exists() or omniroute_env,
        "config_path": str(omniroute_home if omniroute_home.exists() else root / "verdict.yaml"),
        "session_dir": str(omniroute_home),
    }

    # 8. GitHub CLI & Workflows
    gh_cli = bool(shutil.which("gh"))
    github_dir = root / ".github"
    tools["github"] = {
        "name": "GitHub CLI & Workflows",
        "installed": gh_cli or github_dir.exists(),
        "config_path": str(github_dir if github_dir.exists() else root),
        "session_dir": str(github_dir / "workflows"),
    }

    # 9. MCP Servers (.mcp.json)
    mcp_local = root / ".mcp.json"
    mcp_global = home / ".mcp.json"
    tools["mcp"] = {
        "name": "MCP Server Registry (.mcp.json)",
        "installed": mcp_local.exists() or mcp_global.exists(),
        "config_path": str(mcp_local if mcp_local.exists() else mcp_global),
        "session_dir": str(mcp_local.parent),
    }

    preselected = tuple(name for name, info in tools.items() if info["installed"])

    return ToolDetectionReport(detected_tools=tools, preselected_tools=preselected)


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
            claude_md = root / "CLAUDE.md"
            existing = claude_md.read_text("utf-8") if claude_md.exists() else ""
            if "Verdict Unified Memory Bridge" not in existing:
                claude_md.write_text(existing + bridge_instruction, encoding="utf-8")

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

        elif tool == "cursor_jcode":
            cursor_file = root / ".cursorrules"
            existing = cursor_file.read_text("utf-8") if cursor_file.exists() else ""
            if "Verdict Unified Memory Bridge" not in existing:
                cursor_file.write_text(existing + bridge_instruction, encoding="utf-8")
            configured.append("cursor_jcode")

        elif tool in {"mcp", "all"}:
            mcp_file = root / ".mcp.json"
            mcp_data: dict[str, Any] = {"mcpServers": {}}
            if mcp_file.exists():
                try:
                    mcp_data = json.loads(mcp_file.read_text("utf-8"))
                except Exception:
                    mcp_data = {"mcpServers": {}}

            servers = mcp_data.setdefault("mcpServers", {})
            servers["verdict-memory"] = {
                "command": "uv",
                "args": ["run", "-m", "verdict.cli", "serve"],
                "env": {
                    "VERDICT_MEMORY_PLANE_PATH": str(shared_db),
                    "VERDICT_GUIDANCE_ENABLED": "1",
                },
            }
            mcp_file.write_text(json.dumps(mcp_data, indent=2), encoding="utf-8")
            configured.append("mcp")

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
        self, task_id: str, status: str = "complete", summary: str = ""
    ) -> dict[str, Any]:
        """After task completion: log outcome receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="outcome", scope=task_id, payload={"status": status, "summary": summary}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 3. File & Edit Hooks
    def on_file_edit_start(self, file_path: str, *, implementation: bool = False) -> dict[str, Any]:
        """Before file edit: validate path safety against quarantine rules."""
        norm = file_path.replace("\\", "/").lower()
        if "/tmp" in norm or "\\tmp" in norm or norm.startswith("/tmp"):  # nosec B108: deliberate quarantine check
            raise ValueError(f"quarantined_path_rejected:{file_path}")

        preflight = None
        if implementation:
            from verdict.documentation_preflight import require_documentation_preflight

            preflight = require_documentation_preflight(memory_path=self.plane.path)

        rec = self.receipt_store.put_receipt(
            receipt_type="execution",
            scope=f"edit_start:{file_path}",
            payload={"path": file_path, "action": "pre_edit_check"},
        )
        result: dict[str, Any] = {"status": "success", "receipt_id": rec.receipt_id}
        if preflight is not None:
            result["documentation_preflight"] = preflight.to_dict()
        return result

    def on_file_edit_complete(self, file_path: str, diff_hash: str = "") -> dict[str, Any]:
        """After file edit: log provenance receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="execution",
            scope=f"edit_complete:{file_path}",
            payload={"path": file_path, "diff_hash": diff_hash},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 4. Command Execution Hooks
    def on_command_execute(self, cmd_str: str) -> dict[str, Any]:
        """Before command execution: perform security scanning for destructive commands."""
        destructive_patterns = [r"rm\s+-rf\s+/", r"rm\s+-rf\s+~", r"rm\s+-rf\s+\$HOME", r"mkfs"]
        for pat in destructive_patterns:
            if re.search(pat, cmd_str):
                raise ValueError(f"destructive_command_rejected:{cmd_str}")

        rec = self.receipt_store.put_receipt(
            receipt_type="execution", scope="pre_command", payload={"command": cmd_str}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_command_complete(
        self, cmd_str: str, exit_code: int, duration_ms: float = 0.0
    ) -> dict[str, Any]:
        """After command execution: log output receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="execution",
            scope="post_command",
            payload={"command": cmd_str, "exit_code": exit_code, "duration_ms": duration_ms},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 5. Session Lifecycle Hooks
    def on_session_start(self, session_id: str, project_scope: str = "default") -> dict[str, Any]:
        """On session start: log context receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="context",
            scope=session_id,
            payload={"action": "session_start", "project_scope": project_scope},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_session_end(
        self,
        session_id: str,
        transcript: list[dict[str, Any]],
        receipts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """On session end: store transcript records to MemoryPlane and receipts to ReceiptStore."""
        stored_count = 0
        for idx, item in enumerate(transcript):
            content = item.get("content") or json.dumps(item)
            result = self.gate.write(
                MemoryWriteRequest(
                    namespace="session_history",
                    key=f"{session_id}:item_{idx}",
                    value=content,
                    authority="session_adapter",
                    confidence=1.0,
                    provenance={"source": f"session:{session_id}", "item_index": idx},
                    scope=session_id,
                )
            )
            if result.allowed:
                stored_count += 1

        receipt_count = 0
        if receipts:
            for r in receipts:
                self.receipt_store.put_receipt(
                    receipt_type=r.get("receipt_type", "execution"),
                    scope=r.get("scope", session_id),
                    payload=r.get("payload", {}),
                    sensitivity=r.get("sensitivity", "internal"),
                )
                receipt_count += 1

        return {
            "status": "success",
            "session_id": session_id,
            "transcript_records_stored": stored_count,
            "receipts_logged": receipt_count,
        }

    def on_session_restore(self, session_id: str) -> dict[str, Any]:
        """On session restore: log restore receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="context", scope=session_id, payload={"action": "session_restore"}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 6. Verification & Error Hooks
    def on_verify(
        self, target: str, status: str = "passed", details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Before promotion: log verification receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="verification",
            scope=f"verify:{target}",
            payload={"target": target, "status": status, "details": details or {}},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_error(self, error_msg: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """On error: log outcome receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="outcome",
            scope="error_handler",
            payload={"error": error_msg, "context": context or {}},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}


__all__ = [
    "MemoryHookController",
    "ToolDetectionReport",
    "configure_memory_bridge",
    "detect_available_tools",
]


def run_doctor_diagnostics(
    home_dir: Path | None = None, cwd: Path | None = None, fix: bool = False
) -> dict[str, Any]:
    """Scan system health, memory plane databases, MCP entries, tool bridge headers, and auto-repair if fix=True."""
    home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()

    issues: list[str] = []
    repaired: list[str] = []
    v_dir = home / ".verdict"
    memory_db = v_dir / "memory.db"
    had_verdict_dir = v_dir.exists()
    had_memory_db = memory_db.exists()

    from verdict.documentation_preflight import run_documentation_preflight
    from verdict.memory_adapters import build_default_adapter_registry

    adapter_registry = build_default_adapter_registry()
    adapter_capabilities = adapter_registry.status()

    documentation_report = run_documentation_preflight(
        repo_root=root, memory_path=home / ".verdict" / "memory.db", fix=fix
    )
    if not documentation_report.passed:
        issues.append("authoritative documentation preflight did not pass")
        issues.extend(documentation_report.errors)
    elif fix and documentation_report.ingested:
        repaired.append("authoritative documentation preflight repaired")
    if fix and not had_verdict_dir and v_dir.exists():
        repaired.append("created_verdict_dir")
    if fix and not had_memory_db and memory_db.exists():
        repaired.append("initialized_memory_db")

    # 1. Check .verdict directory
    if not v_dir.exists():
        issues.append("missing_verdict_dir")
        if fix:
            v_dir.mkdir(parents=True, exist_ok=True)
            repaired.append("created_verdict_dir")

    # 2. Check Memory database
    db_path = memory_db
    if not db_path.exists():
        issues.append("missing_memory_db")
        if fix:
            MemoryPlane(db_path)
            repaired.append("initialized_memory_db")

    # 3. Check MCP server entry
    mcp_file = root / ".mcp.json"
    if not mcp_file.exists():
        issues.append("missing_mcp_config")
        if fix:
            configure_memory_bridge(["mcp"], home_dir=home, cwd=root)
            repaired.append("created_mcp_config")

    # 4. Check tool bridges
    report = detect_available_tools(home_dir=home, cwd=root)
    for tool in report.preselected_tools:
        if tool == "codex" and not (root / ".codex" / "AGENTS.md").exists():
            issues.append("missing_codex_bridge")
            if fix:
                configure_memory_bridge(["codex"], home_dir=home, cwd=root)
                repaired.append("fixed_codex_bridge")

    documentation_ready = documentation_report.passed
    return {
        "status": "healthy" if not issues and documentation_ready else "issues_found",
        "issues": issues,
        "repaired": repaired,
        "fix_applied": fix,
        "documentation_preflight": documentation_report.to_dict(),
        "memory_adapters": {"protocol_version": "1", "capabilities": adapter_capabilities},
    }


def uninstall_memory_bridge(
    home_dir: Path | None = None, cwd: Path | None = None, purge_data: bool = False
) -> dict[str, Any]:
    """Reversibly strip tool memory bridge headers and MCP entries, preserving source code and data by default."""
    _home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()

    uninstalled: list[str] = []

    for file_path in [root / ".codex" / "AGENTS.md", root / "CLAUDE.md", root / ".cursorrules"]:
        if file_path.exists():
            content = file_path.read_text("utf-8")
            if "# Verdict Unified Memory Bridge" in content:
                parts = content.split("# Verdict Unified Memory Bridge")
                file_path.write_text(parts[0].rstrip(), encoding="utf-8")
                uninstalled.append(file_path.name)

    mcp_file = root / ".mcp.json"
    if mcp_file.exists():
        try:
            data = json.loads(mcp_file.read_text("utf-8"))
            if "mcpServers" in data and "verdict-memory" in data["mcpServers"]:
                del data["mcpServers"]["verdict-memory"]
                mcp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                uninstalled.append(".mcp.json")
        except Exception:
            pass

    if purge_data:
        v_dir = root / ".verdict"
        if v_dir.exists():
            shutil.rmtree(v_dir, ignore_errors=True)
            uninstalled.append(".verdict_data_purged")

    return {"status": "success", "uninstalled_targets": uninstalled, "data_purged": purge_data}


__all__.extend(["run_doctor_diagnostics", "uninstall_memory_bridge"])
