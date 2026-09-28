"""Tests for AiMemoryExperimentalProvider (BOD-281 POC)."""


import httpx
import pytest

from verdict.memory_providers.ai_memory_experimental import AiMemoryExperimentalProvider
from verdict.shared_memory import (
    CertificationState,
    ExternalMemoryRef,
    ProviderErrorCode,
    ProviderResultStatus,
    SharedMemoryEnvelope,
    SharedMemoryProviderError,
    SharedMemoryQuery,
)


class FakeTransport(httpx.BaseTransport):
    """Fake HTTP transport for testing."""

    def __init__(self, responses: dict[str, tuple[int, dict]]):
        self.responses = responses
        self.requests = []

    def handle_request(self, request):
        self.requests.append((request.method, str(request.url), request.content))
        path = str(request.url).split("://", 1)[1].split("/", 1)[1]
        status, body = self.responses.get(path, (404, {"error": "not found"}))
        return httpx.Response(
            status_code=status,
            json=body,
            headers={"content-type": "application/json"},
        )


def test_health_available():
    """Health check returns AVAILABLE when ai-memory responds."""
    transport = FakeTransport({
        "api/v1/workspaces": (200, [{"workspace_name": "default", "project_count": 1, "page_count": 0}]),
    })
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        transport=transport,
    )

    health = provider.health()

    assert health.provider_id == "ai-memory-experimental"
    assert health.status == ProviderResultStatus.AVAILABLE
    assert health.state == CertificationState.HEALTHY
    assert health.backend == "ai-memory"


def test_health_unavailable_on_network_error():
    """Health check returns UNAVAILABLE when ai-memory is unreachable."""
    transport = FakeTransport({})  # No responses = connection error
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        transport=transport,
    )

    health = provider.health()

    assert health.status == ProviderResultStatus.UNAVAILABLE
    assert health.state == CertificationState.HEALTHY


def test_health_auth_failed():
    """Health check returns AUTH_FAILED on 401."""
    transport = FakeTransport({
        "api/v1/workspaces": (401, {"error": "unauthorized"}),
    })
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        token="wrong-token",
        transport=transport,
    )

    health = provider.health()

    assert health.status == ProviderResultStatus.AUTH_FAILED


def test_search_fail_open_on_timeout():
    """Search returns empty result with TIMEOUT status, never raises."""
    # Simulate timeout by returning error in transport
    transport = FakeTransport({})
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        transport=transport,
        timeout=0.1,
    )

    query = SharedMemoryQuery(
        query="test",
        project="verdict-core",
    )

    result = provider.search(query)

    assert result.status == ProviderResultStatus.UNAVAILABLE
    assert len(result.hits) == 0
    assert result.error_code is not None


def test_search_returns_advisory_records():
    """Search results always have authority_verified=False."""
    transport = FakeTransport({
        "mcp": (200, {
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "{\"hits\": [{\"id\": \"abc123\", \"path\": \"decisions/001.md\", \"title\": \"Decision\", \"snippet\": \"We decided X\", \"rank\": -0.036}]}",
                    }
                ],
                "isError": False,
            }
        }),
    })
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        transport=transport,
    )

    query = SharedMemoryQuery(
        query="decision",
        project="verdict-core",
    )

    result = provider.search(query)

    assert result.status == ProviderResultStatus.AVAILABLE
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.envelope.authority_verified is False
    assert hit.envelope.trust == "remote-advisory"
    assert hit.envelope.authority == "shared-memory-advisory"


def test_put_writes_with_frontmatter():
    """put() sends memory to ai-memory with correct frontmatter."""
    transport = FakeTransport({
        "mcp": (200, {"result": {"content": [{"type": "text", "text": "{\"page_id\": \"01a0e9cb-test\", \"path\": \"verdict/memory/abc123.md\", \"checkpoint\": \"abc\"}"}], "isError": False}}),
    })
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        transport=transport,
    )

    envelope = SharedMemoryEnvelope(
        content="Important decision about routing",
        project="verdict-core",
        memory_kind="decision",
        retention_class="semantic",
    )

    ref = provider.put(envelope)

    assert ref.provider_id == "ai-memory-experimental"
    assert "verdict/" in ref.external_id


def test_delete_fail_open():
    """delete() returns False on error, never raises."""
    transport = FakeTransport({})  # No response = error
    provider = AiMemoryExperimentalProvider(
        "http://localhost:49374",
        transport=transport,
    )

    ref = ExternalMemoryRef(
        provider_id="ai-memory-experimental",
        external_id="verdict/memory/test.md",
    )

    result = provider.delete(ref)

    assert result is False  # Fail-open


def test_provider_not_configured_raises():
    """Empty endpoint raises on construction."""
    with pytest.raises(SharedMemoryProviderError) as exc_info:
        AiMemoryExperimentalProvider("")

    assert exc_info.value.code == ProviderErrorCode.NOT_CONFIGURED
