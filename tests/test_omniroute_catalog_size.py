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
