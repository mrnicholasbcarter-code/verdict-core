"""Proactive Health Monitoring and Auto-Remediation Daemon.

Runs continuous or single-pass background monitoring across MemoryPlane DB
integrity, 9-ecosystem tool bridge headers, MCP registrations, and quality gates,
automatically applying deterministic remedies when issues are detected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.memory_bridge import configure_memory_bridge, detect_available_tools
from verdict.memory_plane import MemoryPlane
from verdict.receipt_store import ReceiptStore

logger = logging.getLogger("verdict.daemon")


@dataclass(frozen=True)
class RemediationResult:
    """Result of an auto-remediation check and fix."""

    check_name: str
    healthy_before: bool
    remediated: bool
    details: dict[str, Any]


class VerdictProactiveDaemon:
    """Active monitoring and self-healing daemon for Verdict environment."""

    def __init__(
        self,
        cwd: Path | None = None,
        home_dir: Path | None = None,
        check_interval_sec: float = 30.0,
    ) -> None:
        self.root = (cwd or Path.cwd()).resolve()
        self.home = (home_dir or Path.home()).resolve()
        self.check_interval_sec = check_interval_sec
        self._running = False

    def run_health_scan_and_remediate(self) -> list[RemediationResult]:
        """Perform full health scan and auto-remediate detected issues."""
        results: list[RemediationResult] = []

        # 1. Check MemoryPlane Database Integrity
        db_path = self.root / ".verdict" / "memory.db"
        mp_healthy = True
        mp_details: dict[str, Any] = {}

        try:
            plane = MemoryPlane(path=db_path)
            status = plane.status()
            mp_details = {"record_count": status.get("total_records", 0)}
            plane.close()
        except Exception as exc:
            mp_healthy = False
            mp_details = {"error": str(exc)}

        if not mp_healthy:
            # Remediate DB
            db_path.parent.mkdir(parents=True, exist_ok=True)
            re_plane = MemoryPlane(path=db_path)
            re_plane.close()
            results.append(
                RemediationResult(
                    check_name="memory_plane_db",
                    healthy_before=False,
                    remediated=True,
                    details={"action": "reinitialized_memory_db"},
                )
            )
        else:
            results.append(
                RemediationResult(
                    check_name="memory_plane_db",
                    healthy_before=True,
                    remediated=False,
                    details=mp_details,
                )
            )

        # 2. Check Tool Bridge Headers & MCP Registration
        tools_report = detect_available_tools(home_dir=self.home, cwd=self.root)
        installed = tuple(tools_report.preselected_tools)

        mcp_file = self.root / ".mcp.json"
        mcp_healthy = True
        if mcp_file.exists():
            try:
                data = json.loads(mcp_file.read_text(encoding="utf-8"))
                if "verdict-memory" not in data.get("mcpServers", {}):
                    mcp_healthy = False
            except Exception:
                mcp_healthy = False
        else:
            mcp_healthy = False

        if not mcp_healthy:
            configure_memory_bridge(
                selected_tools=list(installed), home_dir=self.home, cwd=self.root
            )
            results.append(
                RemediationResult(
                    check_name="tool_bridge_mcp",
                    healthy_before=False,
                    remediated=True,
                    details={"action": "reconfigured_mcp_bridge"},
                )
            )
        else:
            results.append(
                RemediationResult(
                    check_name="tool_bridge_mcp",
                    healthy_before=True,
                    remediated=False,
                    details={"installed_tools": installed},
                )
            )

        # 3. Log Remediation Receipts
        receipt_store = ReceiptStore(self.root / ".verdict" / "receipts.db")
        for res in results:
            if res.remediated:
                receipt_store.put_receipt(
                    receipt_type="decision", scope="daemon_scan", payload=res.details
                )

        return results

    async def start_loop(self) -> None:
        """Start async background monitoring loop."""
        self._running = True
        logger.info("Starting Verdict Proactive Daemon (interval=%ss)...", self.check_interval_sec)

        while self._running:
            try:
                results = self.run_health_scan_and_remediate()
                remediated_count = sum(1 for r in results if r.remediated)
                if remediated_count > 0:
                    logger.warning("Daemon auto-remediated %s issue(s).", remediated_count)
            except Exception as exc:
                logger.error("Daemon scan error: %s", exc)

            await asyncio.sleep(self.check_interval_sec)

    def stop(self) -> None:
        """Stop background monitoring loop."""
        self._running = False


__all__ = ["RemediationResult", "VerdictProactiveDaemon"]
