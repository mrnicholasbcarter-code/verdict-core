"""BOD-423: client-side, fail-closed redaction of bare ``sk-`` tokens before transport.

Fake tokens are assembled at runtime so no secret-shaped literal sits in the
source. No network: every request goes to an ``httpx.MockTransport`` that
records the raw bytes it was handed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict_memory import contracts
from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
from verdict_memory.contracts import (
    EventIdentity,
    LifecycleEvent,
    MemoryProviderError,
    ProviderErrorCode,
    ProviderResultStatus,
    RecallQuery,
    guard_outbound,
    redact_secrets,
)
from verdict_memory.outbox import MemoryOutbox
from verdict_memory.policy import MemoryCapturePolicy

_PREFIX = "s" + "k-"


def _alnum_token() -> str:
    return _PREFIX + "test" + "0123456789abcdefFAKE"


def _segmented_token() -> str:
    return _PREFIX + "proj-" + "test_FAKE-" + "0123456789abcdef"


def _ant_token() -> str:
    return _PREFIX + "ant-api03-" + "testFAKE" + "9876543210"


ALL_TOKENS = (_alnum_token, _segmented_token, _ant_token)


class _Recorder:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    def transport(self) -> httpx.MockTransport:
        def _dispatch(request: httpx.Request) -> httpx.Response:
            self.bodies.append(bytes(request.content))
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
            text = json.dumps({"hits": []})
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [{"type": "text", "text": text}]},
                },
            )

        return httpx.MockTransport(_dispatch)

    @property
    def wire(self) -> bytes:
        return b"\n".join(self.bodies)


def _client(rec: _Recorder, tmp_path: Path) -> AiMemoryClient:
    return AiMemoryClient(
        AiMemoryClientConfig(
            endpoint="http://127.0.0.1:49374",
            token="test-token-not-real",
            token_file=None,
            workspace="default",
            project="bod423-test",
            transport=rec.transport(),
            writer_lock_dir=tmp_path / "locks",
        )
    )


def _event(**overrides: Any) -> LifecycleEvent:
    base: dict[str, Any] = {
        "kind": "manual_capture",
        "content": "safe content",
        "identity": EventIdentity(
            source_harness="verdict-memory-test",
            project="bod423-test",
            session_id="sess-a",
            source_event_id="src-1",
            workspace="default",
        ),
        "created_at": 1_700_000_000.0,
    }
    base.update(overrides)
    return LifecycleEvent(**base)


@pytest.mark.parametrize("make", ALL_TOKENS)
def test_redact_secrets_removes_bare_sk_tokens(make: Any) -> None:
    token = make()
    out = redact_secrets(f"echo {token} done")
    assert out is not None
    assert token not in out
    assert "[REDACTED]" in out
    assert _PREFIX not in out


def test_redact_secrets_keeps_hyphenated_prose() -> None:
    prose = "use " + _PREFIX + "learn-compatible-estimators here"
    assert redact_secrets(prose) == prose


@pytest.mark.parametrize("make", ALL_TOKENS)
def test_policy_denies_bare_sk_in_content(make: Any) -> None:
    decision = MemoryCapturePolicy().evaluate(_event(content=f"note {make()}"))
    assert decision.eligible is False
    assert decision.reason == "secret_detected"


@pytest.mark.parametrize("make", ALL_TOKENS)
def test_query_token_is_redacted_on_the_wire(make: Any, tmp_path: Path) -> None:
    token = make()
    rec = _Recorder()
    client = _client(rec, tmp_path)
    result = client.search(
        RecallQuery(query=f"find {token}", project="bod423-test", workspace="default")
    )
    assert result.status == ProviderResultStatus.AVAILABLE
    assert rec.bodies, "the request must still go out once redacted"
    assert token.encode() not in rec.wire
    assert _PREFIX.encode() not in rec.wire
    assert b"[REDACTED]" in rec.wire


def test_nested_keys_and_values_are_redacted_by_guard() -> None:
    token = _segmented_token()
    guarded = guard_outbound({"params": {"arguments": {token: [token, {"x": token}]}}})
    blob = json.dumps(guarded)
    assert token not in blob
    assert blob.count("[REDACTED]") == 3


def test_content_secret_is_denied_before_any_request(tmp_path: Path) -> None:
    rec = _Recorder()
    client = _client(rec, tmp_path)
    with pytest.raises(MemoryProviderError) as info:
        client.put(_event(content=f"note {_segmented_token()}"))
    assert info.value.code == ProviderErrorCode.INVALID_REQUEST
    assert rec.bodies == []


def test_guard_error_fails_closed_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(value: Any) -> Any:
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr(contracts, "_redact_bare_sk", _boom)
    rec = _Recorder()
    client = _client(rec, tmp_path)
    token = _alnum_token()
    with pytest.raises(MemoryProviderError) as info:
        client._tool("memory_query", {"query": token})
    assert info.value.code == ProviderErrorCode.INVALID_REQUEST
    assert "request not sent" in info.value.safe_message
    assert token not in str(info.value)
    assert rec.bodies == []
    assert client.last_error is not None
    assert client.last_error["code"] == ProviderErrorCode.INVALID_REQUEST.value
    assert token not in json.dumps(client.last_error)


def test_guard_noop_redactor_is_caught_by_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contracts, "_redact_bare_sk", lambda value: value)
    rec = _Recorder()
    client = _client(rec, tmp_path)
    token = _ant_token()
    with pytest.raises(MemoryProviderError, match="still holds a bare sk- token"):
        client._tool("memory_query", {"query": token})
    assert rec.bodies == []


def test_guard_rejects_uncheckable_payload(tmp_path: Path) -> None:
    rec = _Recorder()
    client = _client(rec, tmp_path)
    with pytest.raises(MemoryProviderError, match="could not check payload"):
        client._tool("memory_query", {"query": object()})
    assert rec.bodies == []


def test_search_guard_failure_is_typed_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(contracts, "_redact_bare_sk", lambda value: value)
    rec = _Recorder()
    client = _client(rec, tmp_path)
    result = client.search(
        RecallQuery(query=f"find {_alnum_token()}", project="bod423-test", workspace="default")
    )
    assert result.status != ProviderResultStatus.AVAILABLE
    assert result.error_code == ProviderErrorCode.INVALID_REQUEST
    assert rec.bodies == []


def test_outbox_redacts_segmented_token_in_metadata(tmp_path: Path) -> None:
    token = _segmented_token()
    db = tmp_path / "outbox.sqlite"
    outbox = MemoryOutbox(db)
    outbox.enqueue(_event(metadata={"nested": {"note": f"key {token}"}}))
    with sqlite3.connect(db) as conn:
        raw = json.dumps(
            [list(r) for r in conn.execute("select * from memory_lifecycle_outbox").fetchall()]
        )
    assert token not in raw
