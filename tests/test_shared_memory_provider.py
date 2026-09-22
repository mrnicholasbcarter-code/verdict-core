"""Offline contract tests for SharedMemoryProvider (BOD-145 / MEMORY M1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict.shared_memory import (
    ENVELOPE_SCHEMA_VERSION,
    CertificationState,
    FakeSharedMemoryProvider,
    ProviderErrorCode,
    ProviderResultStatus,
    SharedMemoryEnvelope,
    SharedMemoryProviderError,
    SharedMemoryQuery,
    discover_shared_memory_setup,
    doctor_shared_memory_report,
    normalize_hit_to_memory_record,
    redact_secrets,
)
from verdict.shared_memory_mcp import MCPMemoryServiceProvider

CANARY = "canary-secret-TOKEN-9f3c2b1a"


def _envelope(**overrides: Any) -> SharedMemoryEnvelope:
    base = {
        "content": "shared note about routing budgets",
        "project": "verdict",
        "scope": "default",
        "tenant": "default",
        "created_at": 1_700_000_000.0,
        "observed_at": 1_700_000_001.0,
        "source_agent": "worker-a",
        "metadata": {"extension": {"k": 1}},
    }
    base.update(overrides)
    return SharedMemoryEnvelope(**base)


def test_envelope_roundtrip() -> None:
    first = _envelope(metadata={"b": 2, "a": 1})
    second = SharedMemoryEnvelope.from_dict(first.to_dict())
    assert second == first
    assert first.schema_version == ENVELOPE_SCHEMA_VERSION
    assert first.content_hash == second.content_hash
    # Equivalent reordered metadata yields the same digest.
    twin = _envelope(metadata={"a": 1, "b": 2}, observed_at=9_999.0)
    assert twin.digest() == first.digest()
    assert twin.idempotency_key == first.digest()


def test_fake_provider_roundtrip_and_scope_filter() -> None:
    provider = FakeSharedMemoryProvider()
    kept = provider.put(_envelope(content="alpha budget note", project="verdict", scope="alpha"))
    provider.put(_envelope(content="beta budget note", project="verdict", scope="beta"))
    provider.put(_envelope(content="other project", project="other", scope="alpha"))

    alpha = provider.search(SharedMemoryQuery("budget", project="verdict", scope="alpha"))
    assert alpha.status is ProviderResultStatus.AVAILABLE
    assert len(alpha.hits) == 1
    assert alpha.hits[0].ref.external_id == kept.external_id

    empty = provider.search(SharedMemoryQuery("missing-term", project="verdict", scope="alpha"))
    assert empty.status is ProviderResultStatus.AVAILABLE
    assert empty.hits == ()


def test_normalize_never_grants_authority() -> None:
    provider = FakeSharedMemoryProvider()
    ref = provider.put(
        _envelope(authority_verified=True, authority="spoofed-authority", content="advisory only")
    )
    hit = provider.search(SharedMemoryQuery("advisory", project="verdict")).hits[0]
    assert hit.ref == ref
    record = normalize_hit_to_memory_record(hit)
    assert record.authority_verified is False
    assert record.authority == "shared-memory-advisory"
    assert record.provenance is not None
    assert record.provenance["provider_id"] == provider.provider_id
    assert record.provenance["external_id"] == ref.external_id


def test_empty_search_distinct_from_timeout_and_unavailable() -> None:
    available = FakeSharedMemoryProvider()
    empty = available.search(SharedMemoryQuery("nothing-here", project="verdict"))
    assert empty.status is ProviderResultStatus.AVAILABLE
    assert empty.hits == ()

    timed_out = FakeSharedMemoryProvider(status=ProviderResultStatus.TIMEOUT)
    timeout = timed_out.search(SharedMemoryQuery("x", project="verdict"))
    assert timeout.status is ProviderResultStatus.TIMEOUT
    assert timeout.error_code is ProviderErrorCode.TIMEOUT
    assert timeout.hits == ()

    down = FakeSharedMemoryProvider(status=ProviderResultStatus.UNAVAILABLE)
    unavailable = down.search(SharedMemoryQuery("x", project="verdict"))
    assert unavailable.status is ProviderResultStatus.UNAVAILABLE
    assert unavailable.hits == ()


def test_auth_failure_named_state() -> None:
    provider = FakeSharedMemoryProvider(required_token="expected", token="wrong")
    health = provider.health()
    assert health.status is ProviderResultStatus.AUTH_FAILED
    result = provider.search(SharedMemoryQuery("x", project="verdict"))
    assert result.status is ProviderResultStatus.AUTH_FAILED
    assert result.error_code is ProviderErrorCode.AUTH_FAILED


def test_incompatible_schema_named_state() -> None:
    with pytest.raises(SharedMemoryProviderError) as raised:
        _envelope(schema_version="shared-memory-envelope/v0")
    assert raised.value.code is ProviderErrorCode.SCHEMA_INCOMPATIBLE


def test_setup_discovery_never_installs(tmp_path: pytest.TempPathFactory | Any) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    home.mkdir()
    cwd.mkdir()
    discovery = discover_shared_memory_setup(env={}, home_dir=home, cwd=cwd)
    assert discovery["state"] == CertificationState.NOT_INSTALLED.value
    assert discovery["install_action"] == "none"
    assert discovery["mutated"] is False
    assert list(home.iterdir()) == []
    assert list(cwd.iterdir()) == []

    configured = discover_shared_memory_setup(
        env={"VERDICT_SHARED_MEMORY_URL": "https://memory.example/v1"}, home_dir=home, cwd=cwd
    )
    assert configured["state"] == CertificationState.CONFIGURED.value
    assert configured["mutated"] is False


def test_doctor_report_redacts_secrets(tmp_path: Any) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    home.mkdir()
    cwd.mkdir()
    provider = FakeSharedMemoryProvider()
    report = doctor_shared_memory_report(
        env={
            "VERDICT_SHARED_MEMORY_URL": f"https://user:{CANARY}@memory.example/v1",
            "VERDICT_SHARED_MEMORY_TOKEN": CANARY,
        },
        home_dir=home,
        cwd=cwd,
        provider=provider,
    )
    blob = json.dumps(report, sort_keys=True)
    assert CANARY not in blob
    assert "[REDACTED]" in blob or "memory.example" in blob
    assert report["state"] == CertificationState.HEALTHY.value


def test_redact_secrets_canary() -> None:
    text = redact_secrets(f"Authorization: Bearer {CANARY}; token={CANARY}")
    assert CANARY not in text
    assert "[REDACTED]" in text


class _ScriptedTransport(httpx.BaseTransport):
    def __init__(self, responses: dict[tuple[str, str], httpx.Response]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.headers)))
        key = (request.method, request.url.path)
        if key not in self.responses:
            return httpx.Response(404, json={"error": "missing script"})
        return self.responses[key]


def test_mcp_adapter_contract_with_injectable_transport() -> None:
    envelope = _envelope(content="mcp shared hit")
    transport = _ScriptedTransport(
        {
            ("GET", "/api/health"): httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "version": "0.1.0",
                    "backend": "sqlite-vec",
                    "protocol_version": "1",
                },
            ),
            ("POST", "/api/memories"): httpx.Response(200, json={"id": "mem-1", "revision": "1"}),
            ("POST", "/api/memories/search"): httpx.Response(
                200,
                json={
                    "hits": [
                        {"id": "mem-1", "score": 0.9, "envelope": envelope.to_dict()},
                        {
                            "id": "other",
                            "score": 0.8,
                            "envelope": _envelope(
                                content="wrong project", project="other"
                            ).to_dict(),
                        },
                    ]
                },
            ),
            ("DELETE", "/api/memories/mem-1"): httpx.Response(200, json={"ok": True}),
        }
    )
    provider = MCPMemoryServiceProvider(
        "https://memory.example", env={"VERDICT_SHARED_MEMORY_TOKEN": CANARY}, transport=transport
    )
    health = provider.health()
    assert health.status is ProviderResultStatus.AVAILABLE
    assert health.backend == "sqlite-vec"
    ref = provider.put(envelope)
    assert ref.external_id == "mem-1"
    result = provider.search(SharedMemoryQuery("shared", project="verdict"))
    assert result.status is ProviderResultStatus.AVAILABLE
    assert len(result.hits) == 1
    assert result.hits[0].ref.external_id == "mem-1"
    assert provider.delete(ref) is True
    # Auth header present on wire but never leaked by helpers.
    assert any(
        headers.get("authorization") == f"Bearer {CANARY}" for _, _, headers in transport.calls
    )
    assert CANARY not in provider.redacted_endpoint
    assert CANARY not in json.dumps(health.to_dict())


def test_mcp_adapter_timeout_and_incompatible() -> None:
    class _TimeoutTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

    provider = MCPMemoryServiceProvider(
        "https://memory.example", env={}, transport=_TimeoutTransport()
    )
    health = provider.health()
    assert health.status is ProviderResultStatus.TIMEOUT
    search = provider.search(SharedMemoryQuery("x", project="verdict"))
    assert search.status is ProviderResultStatus.TIMEOUT

    bad = MCPMemoryServiceProvider(
        "https://memory.example",
        env={},
        transport=_ScriptedTransport(
            {
                ("GET", "/api/health"): httpx.Response(
                    200, json={"status": "ok", "schema_version": "shared-memory-envelope/v9"}
                )
            }
        ),
    )
    incompatible = bad.health()
    assert incompatible.status is ProviderResultStatus.INCOMPATIBLE
    assert incompatible.state is CertificationState.INCOMPATIBLE


def test_no_generative_dependency_imported() -> None:
    import verdict.shared_memory as shared
    import verdict.shared_memory_mcp as mcp

    for module in (shared, mcp):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "openai" not in source.casefold()
        assert "anthropic" not in source.casefold()
        assert "completion" not in source.casefold()
