"""Tests for Verdict Core stdio MCP Server implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.mcp_server import TOOLS_SCHEMA, VerdictMCPServer


@pytest.mark.asyncio
async def test_mcp_initialize(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    server = VerdictMCPServer(db_path=db_path)
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        res = await server.handle_request(req)
        assert res is not None
        assert res["id"] == 1
        assert res["result"]["serverInfo"]["name"] == "verdict"
        assert res["result"]["protocolVersion"] == "2024-11-05"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_mcp_tools_list(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    server = VerdictMCPServer(db_path=db_path)
    try:
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        res = await server.handle_request(req)
        assert res is not None
        tools = res["result"]["tools"]
        assert len(tools) == len(TOOLS_SCHEMA)
        names = [t["name"] for t in tools]
        assert "verdict_route" in names
        assert "verdict_memory_search" in names
        assert "verdict_memory_store" in names
        assert "verdict_qualify" in names
        assert "verdict_health" in names
    finally:
        server.close()


@pytest.mark.asyncio
async def test_mcp_verdict_route(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    server = VerdictMCPServer(db_path=db_path)
    try:
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "verdict_route",
                "arguments": {"task": "Write secure auth handler", "input_tier": 0},
            },
        }
        res = await server.handle_request(req)
        assert res is not None
        assert res["result"]["isError"] is False
        content = json.loads(res["result"]["content"][0]["text"])
        assert "model_chosen" in content
        assert "provider" in content
    finally:
        server.close()


@pytest.mark.asyncio
async def test_mcp_memory_store_and_search(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    server = VerdictMCPServer(db_path=db_path)
    try:
        # Store memory
        store_req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "verdict_memory_store",
                "arguments": {
                    "key": "test_pattern_auth",
                    "value": "Always use bcrypt or argon2 for password hashing.",
                    "namespace": "default",
                },
            },
        }
        store_res = await server.handle_request(store_req)
        assert store_res is not None
        assert store_res["result"]["isError"] is False

        # Search memory
        search_req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "verdict_memory_search",
                "arguments": {"query": "bcrypt password", "limit": 5},
            },
        }
        search_res = await server.handle_request(search_req)
        assert search_res is not None
        search_data = json.loads(search_res["result"]["content"][0]["text"])
        assert search_data["count"] >= 1
        assert any(r["key"] == "test_pattern_auth" for r in search_data["records"])
    finally:
        server.close()


@pytest.mark.asyncio
async def test_mcp_verdict_qualify(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    server = VerdictMCPServer(db_path=db_path)
    try:
        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "verdict_qualify",
                "arguments": {
                    "model_id": "anthropic/claude-3-5-sonnet",
                    "required_capabilities": ["code_generation"],
                },
            },
        }
        res = await server.handle_request(req)
        assert res is not None
        assert res["result"]["isError"] is False
        report = json.loads(res["result"]["content"][0]["text"])
        assert "passport_digest" in report
        assert "decisions" in report
    finally:
        server.close()


@pytest.mark.asyncio
async def test_mcp_verdict_health(tmp_path: Path) -> None:
    db_path = tmp_path / "test_memory.db"
    server = VerdictMCPServer(db_path=db_path)
    try:
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "verdict_health", "arguments": {}},
        }
        res = await server.handle_request(req)
        assert res is not None
        assert res["result"]["isError"] is False
        health = json.loads(res["result"]["content"][0]["text"])
        assert health["status"] == "healthy"
    finally:
        server.close()
