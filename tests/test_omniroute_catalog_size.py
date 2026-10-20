"""BOD-431: the gateway's live /v1/models catalog exceeds the transport's
1 MiB default and must not be rejected as malformed (serve-catalog-size)."""

from __future__ import annotations

import httpx

from verdict.availability import OmniRouteAvailabilityAdapter
from verdict.availability_cache import AvailabilityCache
from verdict.omniroute import CATALOG_MAX_RESPONSE_BYTES, OmniRouteHTTPTransport

MOCK_ALLOWLIST = {"router.example.test"}


def _large_catalog_payload(min_bytes: int) -> dict[str, object]:
    """Build a valid /v1/models body padded past ``min_bytes`` in O(1) dumps."""
    row = {
        "id": "provider/model-0",
        "provider": "provider",
        "capabilities": ["tool-calling"],
        "description": "x" * min_bytes,
    }
    return {"data": [row]}


def test_catalog_cache_accepts_a_two_megabyte_gateway_response() -> None:
    """A 2 MB /v1/models body (above the 1 MiB default) is read, not malformed."""
    payload = _large_catalog_payload(2 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"status": "healthy"})

    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(handler),
        max_response_bytes=CATALOG_MAX_RESPONSE_BYTES,
    )
    adapter = OmniRouteAvailabilityAdapter(transport)
    cache = AvailabilityCache(source=adapter.evaluate, ttl_seconds=60, stale_window_seconds=30)

    report = cache.get("provider/model-0")

    assert report.errors == ()
    assert any(c.model.id == "provider/model-0" for c in report.candidates)


def test_catalog_transport_still_rejects_a_body_over_the_sixteen_mib_ceiling() -> None:
    oversized_body = b"x" * (CATALOG_MAX_RESPONSE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized_body)

    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(handler),
        max_response_bytes=CATALOG_MAX_RESPONSE_BYTES,
    )
    adapter = OmniRouteAvailabilityAdapter(transport)

    report = adapter.evaluate()

    assert report.errors == ("catalog transport: malformed (response too large)",)


def test_build_availability_cache_uses_a_catalog_safe_transport(monkeypatch) -> None:
    """Goes through the production builder: api._build_availability_cache must
    configure the real OmniRouteHTTPTransport with a response ceiling that
    covers the gateway's live catalog size, not the transport's 1 MiB default."""
    import verdict.api as api

    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    payload = _large_catalog_payload(2 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"status": "healthy"})

    real_transport = OmniRouteHTTPTransport

    def wrapped(*args: object, **kwargs: object) -> OmniRouteHTTPTransport:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_transport(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api, "OmniRouteHTTPTransport", wrapped)

    built = api._build_availability_cache()
    assert built is not None
    cache, _gate = built

    report = cache.get("provider/model-0")

    assert report.errors == ()
    assert any(c.model.id == "provider/model-0" for c in report.candidates)


def test_build_availability_cache_reads_connections_when_health_omits_the_provider(
    monkeypatch,
) -> None:
    """BOD-431 (C): OmniRoute's providerHealth lists only the providers it is
    currently monitoring (live: monitoredCount flips between 41 and 3). The
    serve cache must also read /api/resilience/connections, so an active,
    breaker-closed provider missing from that health window stays admitted.
    A provider with no observation in either source still fails closed."""
    import verdict.api as api
    from verdict.availability import AvailabilityState

    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "mgmt-test-token")

    catalog = {
        "data": [
            {"id": "nara/laguna-s-2.1", "owned_by": "nara"},
            {"id": "mistral/codestral-latest", "owned_by": "mistral"},
            {"id": "ghost/model-x", "owned_by": "ghost"},
        ]
    }
    health = {
        "status": "healthy",
        "providerHealth": {
            "cursor": {"state": "CLOSED"},
            "cursor-api": {"state": "HALF_OPEN"},
            "ollama": {"state": "CLOSED"},
        },
        "providerBreakers": [{"provider": "cursor", "state": "CLOSED"}],
    }
    connections = {
        "connections": [
            {
                "provider": provider,
                "isActive": True,
                "isCoolingDown": False,
                "cooldownRemainingMs": 0,
                "breaker": None,
            }
            for provider in ("nara", "mistral", "codestral", "cursor", "ollama")
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=catalog)
        if request.url.path == "/api/monitoring/health":
            return httpx.Response(200, json=health)
        if request.url.path == "/api/resilience/connections":
            return httpx.Response(200, json=connections)
        return httpx.Response(404, json={"error": "missing"})

    real_transport = OmniRouteHTTPTransport

    def wrapped(*args: object, **kwargs: object) -> OmniRouteHTTPTransport:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_transport(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api, "OmniRouteHTTPTransport", wrapped)

    built = api._build_availability_cache()
    assert built is not None
    cache, _gate = built

    states = {c.model.id: c.state for c in cache.get("nara/laguna-s-2.1").candidates}

    assert states["nara/laguna-s-2.1"] == AvailabilityState.READY
    assert states["mistral/codestral-latest"] == AvailabilityState.READY
    assert states["ghost/model-x"] == AvailabilityState.UNKNOWN


def test_serve_startup_names_health_only_runtime_sources(monkeypatch) -> None:
    """Without a management token, serve's startup diagnostics say why
    providers outside OmniRoute's monitored set read as unknown."""
    import verdict.api as api

    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN", raising=False)
    without = {d["code"]: d for d in api.server_bootstrap_diagnostics()["diagnostics"]}
    assert without["runtime_sources_health_only"]["detail"] == (
        "runtime sources: health only (no management token); providers outside "
        "OmniRoute's monitored set read as unknown"
    )

    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "mgmt-test-token")
    with_token = {d["code"] for d in api.server_bootstrap_diagnostics()["diagnostics"]}
    assert "runtime_sources_health_only" not in with_token
