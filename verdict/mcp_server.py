"""Standard Model Context Protocol (MCP) Stdio JSON-RPC Server for Verdict Core.

Provides high-performance, deterministic MCP tools for AI agents (Codex, Claude Code,
Cursor, Antigravity, etc.) to query routing decisions, capability passports,
MemoryPlane vector memory, qualification reports, context packs, and system health.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.capability_passports import CapabilityPassport, RouteIdentity
from verdict.daemon import VerdictProactiveDaemon
from verdict.documentation_preflight import shared_memory_path
from verdict.gate import Gate
from verdict.memory_bridge import MemoryHookController
from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane
from verdict.qualification_report import build_qualification_report

logger = logging.getLogger("verdict.mcp")

SERVER_NAME = "verdict"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


TOOLS_SCHEMA = [
    {
        "name": "verdict_route",
        "description": (
            "Get deterministic LLM model routing decision, target provider, and capability"
            " qualification for a task or prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task prompt or description to route.",
                },
                "input_tier": {
                    "type": "integer",
                    "description": "Task complexity tier (0=Highest/Opus, 1=High, 2=Medium, 3=Light).",
                    "default": 0,
                },
                "protected": {
                    "type": "boolean",
                    "description": "If true, strictly enforces no-offload critical safety floor.",
                    "default": False,
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "verdict_memory_search",
        "description": (
            "Search Verdict MemoryPlane (local HNSW vector & SQLite memory) for prior session"
            " context, receipts, patterns, and lessons."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic or keyword search query."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memory records to return.",
                    "default": 5,
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace filter (e.g., 'default', 'sessions', 'patterns').",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "verdict_memory_store",
        "description": (
            "Store an operational pattern, session summary, or verified receipt into Verdict"
            " MemoryPlane."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Unique key identifier for the memory record.",
                },
                "value": {
                    "type": "string",
                    "description": "Content text to persist in MemoryPlane.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Namespace category.",
                    "default": "default",
                },
                "sensitivity": {
                    "type": "string",
                    "description": "Data sensitivity level ('public', 'internal', 'confidential').",
                    "default": "internal",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "verdict_qualify",
        "description": (
            "Generate a deterministic capability qualification report for a route and required"
            " capabilities."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {
                    "type": "string",
                    "description": "Model identifier (e.g., 'anthropic/claude-3-5-sonnet').",
                },
                "required_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of required capabilities (e.g., ['code_generation', 'json_output']).",
                },
                "provider": {
                    "type": "string",
                    "description": "Provider name.",
                    "default": "primary",
                },
            },
            "required": ["model_id", "required_capabilities"],
        },
    },
    {
        "name": "verdict_context_pack",
        "description": "Compile a ContextPack slot bundle for a prompt from MemoryPlane.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "User prompt or goal."},
                "max_tokens": {
                    "type": "integer",
                    "description": "Context budget token limit.",
                    "default": 2048,
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "verdict_health",
        "description": (
            "Perform full Verdict environment health scan, memory integrity check, and"
            " auto-remediation."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "verdict_explain",
        "description": (
            "Get detailed deterministic explanation for policy decisions and model selection."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description to explain."}
            },
            "required": ["task"],
        },
    },
    {
        "name": "verdict_code_parse",
        "description": (
            "Parse a source directory into the code-intelligence graph and return the "
            "ingestion report (nodes, edges, files parsed). ADR-005 code intelligence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"root": {"type": "string", "description": "Directory to parse."}},
            "required": ["root"],
        },
    },
    {
        "name": "verdict_code_callers",
        "description": (
            "Find callers / impact radius / tests for a symbol or changed file set in the "
            "code-intelligence graph."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol or file to analyze."},
                "kind": {
                    "type": "string",
                    "enum": ["callers", "callees", "imports", "tests", "impact", "bridge", "hub"],
                    "description": "Analysis to run.",
                },
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For kind=impact: files to compute blast radius for.",
                },
            },
            "required": ["symbol", "kind"],
        },
    },
]


class VerdictMCPServer:
    """Async stdio JSON-RPC MCP Server implementation for Verdict Core."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or shared_memory_path()
        self._plane: MemoryPlane | None = None

    def get_plane(self) -> MemoryPlane:
        if self._plane is None:
            self._plane = MemoryPlane(self.db_path)
        return self._plane

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        req_id = request.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}
        if method == "tools/call":
            params = request.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            try:
                result_text = await self._execute_tool(name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                logger.exception("Error executing MCP tool %s", name)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool error ({name}): {exc}"}],
                        "isError": True,
                    },
                }

        if req_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return None

    async def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "verdict_route":
            task = str(args.get("task", ""))
            input_tier = int(args.get("input_tier", 0))
            crit_map = {0: "critical", 1: "high", 2: "medium", 3: "low"}
            criticality = crit_map.get(input_tier, "medium")
            gate_client = Gate()
            dec = gate_client.route(task, criticality=criticality)
            return json.dumps(
                {
                    "model_chosen": dec.model,
                    "provider": dec.provider,
                    "reason": dec.reason,
                    "task": task,
                    "criticality": criticality,
                },
                indent=2,
            )

        if name == "verdict_memory_search":
            query = str(args.get("query", ""))
            limit = int(args.get("limit", 5))
            namespace = args.get("namespace")
            plane = self.get_plane()
            records = plane.search(query, limit=limit, namespace=namespace)
            res = [
                {
                    "key": r.key,
                    "content": r.content,
                    "namespace": r.namespace,
                    "confidence": r.confidence,
                    "source": r.source,
                }
                for r in records
            ]
            return json.dumps({"count": len(res), "records": res}, indent=2)

        if name == "verdict_memory_store":
            key = str(args.get("key", ""))
            value = str(args.get("value", ""))
            namespace = str(args.get("namespace", "patterns"))
            sensitivity = str(args.get("sensitivity", "internal"))
            provenance = args.get("provenance") or {"source": "verdict_mcp_tool"}
            plane = self.get_plane()
            gate = MemoryGate(plane)
            req = MemoryWriteRequest(
                namespace=namespace,
                key=key,
                value=value,
                authority="agent",
                provenance=provenance,
                sensitivity=sensitivity,
            )
            write_res = gate.write(req)
            return json.dumps(write_res.to_dict(), indent=2)

        if name == "verdict_qualify":
            model_id = str(args.get("model_id", ""))
            req_caps = list(args.get("required_capabilities", []))
            provider = str(args.get("provider", "primary"))
            now = datetime.now(timezone.utc)
            route = RouteIdentity(
                gateway="omniroute",
                provider=provider,
                connection="primary",
                endpoint="https://api.verdict.internal/v1",
                protocol="openai.chat.completions",
                model_id=model_id,
            )
            passport = CapabilityPassport(
                route_identity=route,
                qualified_at=now,
                expires_at=now + timedelta(hours=1),
                claimed={},
                observed={},
            )
            report = build_qualification_report(passport, required_capabilities=req_caps)
            return json.dumps(report.to_dict(), indent=2)

        if name == "verdict_context_pack":
            prompt = str(args.get("prompt", ""))
            budget = int(args.get("max_tokens", 2048))
            plane = self.get_plane()
            controller = MemoryHookController(plane=plane)
            compiled_pack = controller.on_prompt(prompt, context_budget=budget)
            return compiled_pack

        if name == "verdict_health":
            daemon = VerdictProactiveDaemon(cwd=self.db_path.parent, home_dir=self.db_path.parent)
            results = daemon.run_health_scan_and_remediate()
            return json.dumps(
                {"status": "healthy", "remediations": [r.__dict__ for r in results]}, indent=2
            )

        if name == "verdict_explain":
            task = str(args.get("task", ""))
            gate_client = Gate()
            dec = gate_client.route(task, criticality="high")
            explanation = {
                "task": task,
                "model_chosen": dec.model,
                "provider": dec.provider,
                "reason": dec.reason,
            }
            return json.dumps(explanation, indent=2)
        if name == "verdict_code_parse":
            root = str(args.get("root", ""))
            if not root:
                raise ValueError("root is required")
            from verdict.code_graph import CodeGraphEngine

            engine = CodeGraphEngine()
            count = engine.parse_directory(root)
            report = engine.sync_to_memory_plane(self.get_plane())
            return json.dumps({"files_parsed": count, "memory_report": report}, indent=2)
        if name == "verdict_code_callers":
            symbol = str(args.get("symbol", ""))
            kind = str(args.get("kind", "callers"))
            changed_files = args.get("changed_files")
            from verdict.code_graph import CodeGraphEngine

            engine = CodeGraphEngine()
            if kind == "callers":
                result = [n.name for n in engine.callers_of(symbol)]
            elif kind == "callees":
                result = engine.callees_of(symbol)
            elif kind == "imports":
                result = engine.imports_of(symbol)
            elif kind == "tests":
                result = [n.name for n in engine.tests_for(symbol)]
            elif kind == "bridge":
                result = engine.bridge_nodes()
            elif kind == "hub":
                result = engine.hub_nodes()
            elif kind == "impact":
                files = changed_files or [symbol]
                result = sorted(engine.get_impact_radius(files))
            else:
                raise ValueError(f"unknown kind: {kind}")
            return json.dumps({"symbol": symbol, "kind": kind, "result": result}, indent=2)

        raise ValueError(f"Unknown tool: {name}")

    def close(self) -> None:
        if self._plane is not None:
            self._plane.close()
            self._plane = None


async def run_stdio_mcp_server() -> None:
    """Run the stdio MCP JSON-RPC listener loop."""
    server = VerdictMCPServer()
    try:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while not reader.at_eof():
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                request = json.loads(line_str)
                response = await server.handle_request(request)
                if response is not None:
                    out = json.dumps(response) + "\n"
                    sys.stdout.write(out)
                    sys.stdout.flush()
            except Exception as err:
                logger.error("JSON-RPC parse/handle error: %s", err)
    finally:
        server.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(run_stdio_mcp_server())


if __name__ == "__main__":
    main()
