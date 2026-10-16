"""RED mock repros for Sol backend-security findings (c92efa4e / 7ac2bb32).

These tests encode the ORIGINAL mandatory auth/redaction/idempotency/error
contract violations. They MUST fail on the provisional client until the ONE
bounded fix pass lands. No live network. No secrets printed.

Findings (parent/Sol):
1. configured project overwritten by foreign query / unscoped hit relabel
2. distinct harness/session share REMOTE raw source-id path
3. empty write RPC accepted without persisted digest
4. nested metadata secret durable clear (outbox)
5. malformed RPC {} search => available empty (must be typed failure)
6. proof must require exact ref/digest (covered by script; mocked here too)
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
from verdict_memory.contracts import (
    EventIdentity,
    LifecycleEvent,
    ProviderResultStatus,
    RecallQuery,
)
from verdict_memory.outbox import MemoryOutbox


def _cfg(**overrides):
    base = dict(
        endpoint="http://127.0.0.1:49374",
        token="test-token-not-real",
        token_file=None,
        workspace="default",
        project="bod80-memory-test",
        timeout=5.0,
    )
    base.update(overrides)
    return AiMemoryClientConfig(**base)


def _event(**overrides):
    base = dict(
        kind="manual_capture",
        content="safe content",
        identity=EventIdentity(
            source_harness="verdict-memory-proof",
            project="bod80-memory-test",
            session_id="sess-a",
            source_event_id="src-1",
            workspace="default",
        ),
        created_at=1_700_000_000.0,
    )
    base.update(overrides)
    return LifecycleEvent(**base)


def _mcp_ok_init_then(handler):
    """MockTransport: answer initialize/notify; delegate tools/call to handler."""

    def _dispatch(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8") or "{}")
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "ai-memory", "version": "2.6.6"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return handler(request, payload)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload.get("id"), "result": {}})

    return httpx.MockTransport(_dispatch)


def test_red_foreign_query_project_must_not_override_config():
    """Foreign query.project must fail closed before RPC (config authority)."""

    captured: list[dict] = []

    def handler(request, payload):
        args = payload["params"]["arguments"]
        captured.append(args)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": json.dumps({"hits": []})}]},
            },
        )

    client = AiMemoryClient(_cfg(transport=_mcp_ok_init_then(handler)))
    result = client.search(
        RecallQuery(
            query="x",
            project="evil-other-project",
            workspace="default",
            limit=5,
        )
    )
    assert not captured, f"foreign query must not reach MCP; got {captured}"
    assert result.error_code is not None
    assert result.status is not ProviderResultStatus.AVAILABLE


def test_red_unscoped_hit_must_not_relabel_as_configured_project():
    """Hits missing required project/workspace metadata must not be relabeled as config project."""

    def handler(request, payload):
        # Unscoped hit: no project/workspace fields
        hits = [
            {
                "path": "notes/unscoped.md",
                "snippet": "unscoped hit",
                "rank": 1.0,
                # deliberately omit project/workspace
            }
        ]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": json.dumps({"hits": hits})}]},
            },
        )

    client = AiMemoryClient(_cfg(transport=_mcp_ok_init_then(handler)))
    result = client.search(
        RecallQuery(query="unscoped", project="bod80-memory-test", workspace="default")
    )
    # Must NOT present as a trusted in-scope hit for bod80-memory-test.
    # Acceptable: zero hits, OR hits marked with unknown/missing scope (not relabeled).
    for hit in result.hits:
        # Relabel bug: client stamped query.project onto identity.project for unscoped pages.
        assert hit.event.metadata.get("scope_state") == "unknown" or hit.event.identity.project in {
            "",
            "unknown",
        }, (
            "unscoped hit was relabeled as configured project; "
            f"got project={hit.event.identity.project!r} metadata={hit.event.metadata!r}"
        )


def test_red_remote_path_must_include_full_namespaced_identity():
    """Distinct harness/session cannot share a remote source path (real MCP shapes)."""
    from test_transport_contract import Server, event

    server = Server()
    client = server.client()
    e1 = event(
        identity=EventIdentity("harness-a", "allowed", "sess-1", source_event_id="same-raw-id")
    )
    e2 = event(
        identity=EventIdentity("harness-b", "allowed", "sess-2", source_event_id="same-raw-id")
    )
    client.put(e1)
    client.put(e2)
    paths = [args["path"] for args in server.writes]
    assert len(paths) == 2
    assert paths[0] != paths[1], f"remote paths collided for distinct harness/session: {paths}"


def test_red_empty_write_rpc_must_not_ack_without_persisted_digest():
    """Empty/minimal write RPC success without path/digest must not return a trusted ack ref."""

    def handler(request, payload):
        # Empty tool result body — no path, no digest
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": "{}"}]},
            },
        )

    client = AiMemoryClient(_cfg(transport=_mcp_ok_init_then(handler)))
    with pytest.raises(Exception) as ei:
        client.put(_event())
    # Must fail typed — not invent a local path ack
    assert (
        "digest" in str(ei.value).lower()
        or "path" in str(ei.value).lower()
        or "malformed" in str(ei.value).lower()
        or "invalid" in str(ei.value).lower()
    )


def test_red_nested_metadata_secret_redacted_before_durable_outbox(tmp_path: Path):
    """Nested metadata secrets must not be durable-clear in outbox bytes."""
    box = MemoryOutbox(tmp_path / "o.sqlite")
    event = _event(
        content="ok body",
        metadata={
            "nested": {"api_key": "sk-nestedsecrettoken123456", "ok": 1},
            "token": "Bearer nested-cleartext-token",
        },
    )
    row = box.enqueue(event)
    assert row is not None
    raw = (tmp_path / "o.sqlite").read_bytes()
    # Also check stored JSON text
    import sqlite3

    with sqlite3.connect(tmp_path / "o.sqlite") as db:
        blob = db.execute("SELECT event_json FROM memory_lifecycle_outbox").fetchone()[0]
    assert "sk-nestedsecrettoken123456" not in blob
    assert "nested-cleartext-token" not in blob
    assert b"sk-nestedsecrettoken123456" not in raw


def test_red_malformed_rpc_object_must_be_typed_failure_not_healthy_empty():
    """Malformed tool payload {} must yield typed failure, not AVAILABLE+empty hits."""

    def handler(request, payload):
        # Malformed: result content text is empty object with no hits key —
        # or worse, non-object protocol shape. Sol used {} as available-empty bug.
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"content": [{"type": "text", "text": "{}"}]},
            },
        )

    client = AiMemoryClient(_cfg(transport=_mcp_ok_init_then(handler)))
    result = client.search(RecallQuery(query="x", project="bod80-memory-test", workspace="default"))
    assert result.status is not ProviderResultStatus.AVAILABLE or result.error_code is not None, (
        "malformed {} was treated as healthy-empty AVAILABLE"
    )


def test_red_search_hit_exact_ref_and_digest_contract():
    """Hits expose exact verified refs and lifecycle digests, never snippet guesses."""
    from test_transport_contract import Server, event

    server = Server()
    client = server.client()
    first = client.put(
        event(identity=EventIdentity("prime", "allowed", "old", source_event_id="old"))
    )
    exact = event()
    ref = client.put(exact)
    result = client.search(RecallQuery(query="safe", project="allowed"))
    assert result.status is ProviderResultStatus.AVAILABLE
    paths = [hit.ref.external_id for hit in result.hits]
    assert paths == [first.external_id, ref.external_id]
    hit = next(hit for hit in result.hits if hit.ref == ref)
    assert hit.event.digest() == exact.digest()
    assert hit.event.identity.source_event_id == exact.identity.source_event_id
    for hit in result.hits:
        assert hit.untrusted is True
