"""Unified Memory Bridge, Tool Integration Autopilot, and 13-Hook Lifecycle Controller.

Detects available AI tool environments (Codex, Claude Code, Pi, Ruflo, Hermes),
preselects them by default, configures shared memory bridge hooks, and manages
the full 6-category lifecycle hook matrix across prompt, task, file, command,
session, and verification events.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.memory_plane import MemoryPlane, MemoryRecord
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
    """Detect available AI agent tools and preselect installed tools."""
    home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()

    tools: dict[str, dict[str, Any]] = {}

    # Codex
    codex_home = home / ".codex"
    codex_local = root / ".codex"
    codex_installed = codex_home.exists() or codex_local.exists()
    tools["codex"] = {
        "name": "Codex",
        "installed": codex_installed,
        "config_path": str(codex_local if codex_local.exists() else codex_home),
        "session_dir": str(codex_home / "sessions"),
    }

    # Claude Code
    claude_home = home / ".claude"
    claude_md = root / "CLAUDE.md"
    claude_installed = claude_home.exists() or claude_md.exists()
    tools["claude"] = {
        "name": "Claude Code",
        "installed": claude_installed,
        "config_path": str(claude_home if claude_home.exists() else root),
        "session_dir": str(claude_home / "transcripts"),
    }

    # Pi
    pi_home = home / ".pi"
    pi_local = root / ".pi"
    pi_installed = pi_home.exists() or pi_local.exists()
    tools["pi"] = {
        "name": "Pi",
        "installed": pi_installed,
        "config_path": str(pi_local if pi_local.exists() else pi_home),
        "session_dir": str(pi_home / "sessions"),
    }

    # Ruflo / Claude Flow
    ruflo_home = home / ".claude-flow"
    ruflo_local = root / ".claude-flow"
    ruflo_installed = ruflo_home.exists() or ruflo_local.exists() or (root / "ruflo").exists()
    tools["ruflo"] = {
        "name": "Ruflo / Claude Flow",
        "installed": ruflo_installed,
        "config_path": str(ruflo_local if ruflo_local.exists() else ruflo_home),
        "session_dir": str(ruflo_home / "memory"),
    }

    # Hermes
    hermes_home = home / ".hermes"
    hermes_installed = hermes_home.exists()
    tools["hermes"] = {
        "name": "Hermes",
        "installed": hermes_installed,
        "config_path": str(hermes_home),
        "session_dir": str(hermes_home / "history"),
    }

    preselected = tuple(name for name, info in tools.items() if info["installed"])

    return ToolDetectionReport(detected_tools=tools, preselected_tools=preselected)


def configure_memory_bridge(
    selected_tools: list[str],
    plane: MemoryPlane | None = None,
    home_dir: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Configure memory bridge and sync existing sessions into MemoryPlane."""
    home = (home_dir or Path.home()).resolve()
    root = (cwd or Path.cwd()).resolve()
    mem_plane = plane or MemoryPlane(root / ".verdict" / "memory.db")
    session_adapter = SessionAdapter()

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
                            mem_plane.put(session_record_to_memory_record(r))
                        synced_sessions += len(rep.records)
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
                            mem_plane.put(session_record_to_memory_record(r))
                        synced_sessions += len(rep.records)
                    except Exception:
                        continue
            configured.append("claude")

        elif tool == "pi":
            pi_dir = root / ".pi"
            pi_dir.mkdir(parents=True, exist_ok=True)
            pi_cfg = pi_dir / "memory_bridge.json"
            pi_cfg.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "memory_plane": str(root / ".verdict" / "memory.db"),
                        "auto_sync": True,
                    },
                    indent=2,
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
                        "database": str(root / ".verdict" / "memory.db"),
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
                h_cfg.write_text(
                    json.dumps({"memory_plane": str(root / ".verdict" / "memory.db")}),
                    encoding="utf-8",
                )
                configured.append("hermes")

    return {
        "status": "success",
        "configured_tools": configured,
        "synced_session_records": synced_sessions,
        "memory_db_path": str(root / ".verdict" / "memory.db"),
    }


class MemoryHookController:
    """Complete 13-hook controller managing prompt, task, file, command, session, and verification events."""

    def __init__(self, plane: MemoryPlane | None = None, db_path: str | Path | None = None) -> None:
        self.plane = plane or MemoryPlane(db_path or ".verdict/memory.db")
        self.receipt_store = ReceiptStore(
            ":memory:" if not db_path else str(db_path) + "_receipts.db"
        )

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
    def on_task_start(self, task_id: str, goal: str) -> dict[str, Any]:
        """Before task execution: log context start receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="context", scope=task_id, payload={"goal": goal, "task_id": task_id}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    def on_task_complete(
        self, task_id: str, status: str = "complete", summary: str = ""
    ) -> dict[str, Any]:
        """After task completion: log outcome receipt."""
        rec = self.receipt_store.put_receipt(
            receipt_type="outcome", scope=task_id, payload={"status": status, "summary": summary}
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

    # 3. File & Edit Hooks
    def on_file_edit_start(self, file_path: str) -> dict[str, Any]:
        """Before file edit: validate path safety against quarantine rules."""
        norm = file_path.replace("\\", "/").lower()
        if "/tmp" in norm or "\\tmp" in norm or norm.startswith("/tmp"):
            raise ValueError(f"quarantined_path_rejected:{file_path}")

        rec = self.receipt_store.put_receipt(
            receipt_type="execution",
            scope=f"edit_start:{file_path}",
            payload={"path": file_path, "action": "pre_edit_check"},
        )
        return {"status": "success", "receipt_id": rec.receipt_id}

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
            rec = MemoryRecord(
                record_id=f"rec_sess_{session_id}_{idx}",
                namespace="session_history",
                key=f"{session_id}:item_{idx}",
                content=content,
                source=f"session:{session_id}",
                authority="session_adapter",
                confidence=1.0,
                sensitivity="internal",
            )
            self.plane.put(rec)
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
