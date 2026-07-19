from __future__ import annotations

import gzip
import traceback
from datetime import datetime, timezone

import httpx
import pytest

from llm_gate.availability import (
    AvailabilityState,
    CandidateRequirements,
    OmniRouteAvailabilityAdapter,
    OmniRouteTransportError,
    OmniRouteTransportMalformed,
    OmniRouteTransportTimeout,
    OmniRouteTransportUnauthorized,
    OmniRouteTransportUnsupported,
)
from llm_gate.omniroute import OmniRouteHTTPTransport

NOW = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)
MOCK_ALLOWLIST = {"router.example.test"}


def test_http_transport_fetches_exact_catalog_and_health_operations() -> None:
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "p/model",
                            "provider": "p",
                            "capabilities": ["tool-calling"],
                        }
                    ]
                },
            )
        if request.url.path == "/api/monitoring/health":
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "timestamp": NOW.isoformat(),
                    "providerHealth": {"p": {"state": "closed", "failures": 0}},
                    "providerBreakers": [],
                    "lockouts": [],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        api_key="catalog-secret",
        management_token="management-secret",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )
    report = OmniRouteAvailabilityAdapter(transport).evaluate(
        CandidateRequirements(required=frozenset({"tools"})),
        now=NOW,
    )

    assert requests == [
        ("/v1/models", "Bearer catalog-secret"),
        ("/api/monitoring/health", None),
    ]
    assert report.candidates[0].state is AvailabilityState.READY
    assert [candidate.model.id for candidate in report.eligible] == ["p/model"]


def test_management_sources_are_allowlisted_and_minimize_runtime_records() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/monitoring/health":
            assert request.headers.get("authorization") is None
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "timestamp": NOW.isoformat(),
                    "providerHealth": {"p": {"state": "closed"}},
                    "learnedLimits": [{"provider": "p", "remaining": 8, "limit": 10}],
                },
            )
        assert request.headers["authorization"] == "Bearer management-secret"
        if request.url.path == "/api/rate-limits":
            return httpx.Response(
                200,
                json={
                    "connections": [
                        {
                            "provider": "p",
                            "accountId": "must-not-survive",
                            "rateLimitProtection": True,
                            "active": False,
                            "queued": 0,
                            "running": 0,
                        }
                    ],
                    "lockouts": [],
                },
            )
        if request.url.path == "/api/resilience/model-cooldowns":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "provider": "p",
                            "model": "blocked",
                            "remainingMs": 60_000,
                            "failureCount": 2,
                        }
                    ]
                },
            )
        if request.url.path == "/api/usage/budget":
            assert request.url.params["apiKeyId"] == "key-1"
            return httpx.Response(
                200,
                json={
                    "budgetCheck": {
                        "allowed": True,
                        "remaining": 2.5,
                        "secret": "must-not-survive",
                    },
                    "activeLimitUsd": 10,
                },
            )
        if request.url.path == "/api/usage/token-limits":
            assert request.url.params["apiKeyId"] == "key-1"
            return httpx.Response(
                200,
                json={
                    "apiKeyId": "key-1",
                    "limits": [
                        {
                            "apiKeyId": "key-1",
                            "scopeType": "model",
                            "scopeValue": "p/model",
                            "remaining": 0,
                            "tokenLimit": 10_000,
                            "enabled": False,
                        },
                        {
                            "apiKeyId": "key-1",
                            "scopeType": "model",
                            "scopeValue": "p/model",
                            "remaining": 4_096,
                            "tokenLimit": 10_000,
                            "enabled": True,
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    transport = OmniRouteHTTPTransport(
        "https://router.example.test/v1",
        management_token="management-secret",
        usage_api_key_id="key-1",
        runtime_sources={
            "health",
            "rate_limits",
            "model_cooldowns",
            "budget",
            "token_limits",
        },
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )

    runtime = transport.runtime()

    assert paths == [
        "/api/monitoring/health",
        "/api/rate-limits",
        "/api/resilience/model-cooldowns",
        "/api/usage/budget",
        "/api/usage/token-limits",
    ]
    assert runtime["p/model"]["token_headroom"] == 4_096
    assert runtime["p/model"]["budget_remaining"] == 2.5
    assert runtime["p"]["quota_remaining_pct"] == 80.0
    assert runtime["p/blocked"]["health"] == "locked_out"
    assert runtime["p/blocked"]["lockout_until"] == "2026-07-18T18:01:00+00:00"
    assert "accountId" not in repr(runtime)
    assert "must-not-survive" not in repr(runtime)
    assert "secret" not in repr(runtime)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, OmniRouteTransportUnauthorized),
        (403, OmniRouteTransportUnauthorized),
    ],
)
def test_http_transport_unauthorized_body_is_typed_and_redacted(
    status_code: int, expected: type[Exception]
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": "Authorization: Bearer upstream-secret token=secret-value"},
        )

    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        api_key="catalog-secret",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(expected) as captured:
        transport.catalog()

    assert "upstream-secret" not in repr(captured.value)
    assert "secret-value" not in repr(captured.value)
    assert "catalog-secret" not in repr(captured.value)


def test_http_transport_reports_documented_operation_unavailable_without_body() -> None:
    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(404, json={"error": "private-upstream-detail"})
        ),
    )

    with pytest.raises(
        OmniRouteTransportUnsupported, match="documented operation unavailable"
    ) as captured:
        transport.catalog()

    assert "private-upstream-detail" not in repr(captured.value)


def test_http_transport_timeout_and_oversized_payload_are_typed() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Bearer secret-value", request=request)

    with pytest.raises(OmniRouteTransportTimeout):
        OmniRouteHTTPTransport(
            "https://router.example.test",
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(timeout_handler),
        ).catalog()

    oversized = OmniRouteHTTPTransport(
        "https://router.example.test",
        max_response_bytes=16,
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b'{"data":["this is too large"]}')
        ),
    )
    with pytest.raises(OmniRouteTransportMalformed, match="response too large"):
        oversized.catalog()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "file:///tmp/router"},
        {"base_url": "https://user:secret@example.test"},
        {"base_url": "http://127.0.0.1:20128"},
        {
            "base_url": "http://localhost:20128",
            "allow_private_hosts": {"localhost"},
        },
        {
            "base_url": "https://example.test/admin",
            "allow_private_hosts": {"example.test"},
        },
        {
            "base_url": "https://example.test",
            "timeout": 0,
            "allow_private_hosts": {"example.test"},
        },
        {
            "base_url": "https://example.test",
            "timeout": True,
            "allow_private_hosts": {"example.test"},
        },
        {
            "base_url": "https://example.test",
            "max_response_bytes": 0,
            "allow_private_hosts": {"example.test"},
        },
        {
            "base_url": "https://example.test",
            "runtime_sources": {"shell"},
            "allow_private_hosts": {"example.test"},
        },
        {
            "base_url": "https://example.test",
            "runtime_sources": {"token_limits"},
            "management_token": "token",
            "allow_private_hosts": {"example.test"},
        },
        {
            "base_url": "https://example.test",
            "runtime_sources": {"rate_limits"},
            "allow_private_hosts": {"example.test"},
        },
    ],
)
def test_http_transport_rejects_unsafe_or_incomplete_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        OmniRouteHTTPTransport(**kwargs)


def test_loopback_requires_explicit_allowlist() -> None:
    transport = OmniRouteHTTPTransport(
        "http://127.0.0.1:20128/v1",
        allow_private_hosts={"127.0.0.1"},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})),
    )

    assert transport.catalog() == {"data": []}


def test_injected_transport_cannot_bypass_destination_policy() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        OmniRouteHTTPTransport(
            "http://localhost:20128/v1",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})),
        )


def test_plain_http_rejects_non_loopback_even_when_allowlisted() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OmniRouteHTTPTransport(
            "http://8.8.8.8:20128/v1",
            api_key="must-not-be-sent",
            allow_private_hosts={"8.8.8.8"},
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})),
        )


def test_system_health_does_not_mark_unobserved_models_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "p/model", "owned_by": "p"}]})
        return httpx.Response(
            200,
            json={"status": "healthy", "providerHealth": {}, "timestamp": NOW.isoformat()},
        )

    report = OmniRouteAvailabilityAdapter(
        OmniRouteHTTPTransport(
            "https://router.example.test",
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )
    ).evaluate(now=NOW)

    assert report.candidates[0].state is AvailabilityState.UNKNOWN
    assert report.eligible == ()


def test_budget_without_an_active_limit_remains_unknown() -> None:
    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        management_token="management-secret",
        usage_api_key_id="key-1",
        runtime_sources={"budget"},
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "activeLimitUsd": 0,
                    "budgetCheck": {
                        "allowed": True,
                        "remaining": 0,
                        "activeLimitUsd": 0,
                    },
                },
            )
        ),
    )

    assert "budget_remaining" not in transport.runtime()["default"]


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (False, {"budget_remaining": 0.0, "eligible": False}),
        ("false", {}),
        (None, {}),
    ],
)
def test_budget_denial_dominates_and_malformed_allowed_is_ignored(
    allowed: object, expected: dict[str, object]
) -> None:
    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        management_token="management-secret",
        usage_api_key_id="key-1",
        runtime_sources={"budget"},
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "activeLimitUsd": 10,
                    "budgetCheck": {"allowed": allowed, "remaining": 2.5},
                },
            )
        ),
    )

    default = transport.runtime()["default"]
    assert {key: default[key] for key in expected} == expected
    if not expected:
        assert "budget_remaining" not in default
        assert "eligible" not in default


def test_alias_catalog_rows_cannot_bypass_canonical_model_lockout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "cc/claude-opus",
                            "owned_by": "claude-code",
                            "root": "claude-opus",
                        },
                        {
                            "id": "claude-code/claude-opus",
                            "owned_by": "claude-code",
                            "root": "claude-opus",
                        },
                    ]
                },
            )
        if request.url.path == "/api/monitoring/health":
            return httpx.Response(
                200,
                json={
                    "timestamp": NOW.isoformat(),
                    "providerHealth": {"claude-code": {"state": "closed"}},
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "provider": "claude-code",
                        "model": "claude-opus",
                        "remainingMs": 60_000,
                    }
                ]
            },
        )

    report = OmniRouteAvailabilityAdapter(
        OmniRouteHTTPTransport(
            "https://router.example.test",
            management_token="management-secret",
            runtime_sources={"health", "model_cooldowns"},
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(handler),
            clock=lambda: NOW,
        )
    ).evaluate(now=NOW)

    assert {candidate.state for candidate in report.candidates} == {AvailabilityState.LOCKED_OUT}
    assert report.eligible == ()


def test_http_transport_rejects_redirects_and_encoded_responses() -> None:
    redirect = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(lambda _: httpx.Response(302, json={"data": []})),
    )
    with pytest.raises(OmniRouteTransportError, match="http status 302"):
        redirect.catalog()

    def compressed_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=gzip.compress(b'{"data":[]}'),
        )

    encoded = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(compressed_handler),
    )
    with pytest.raises(OmniRouteTransportMalformed, match="encoded"):
        encoded.catalog()


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 2_000 + b"0" + b"]" * 2_000,
        b'{"value":' + b"9" * 5_000 + b"}",
    ],
)
def test_hostile_json_is_a_sanitized_typed_failure(body: bytes) -> None:
    transport = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        max_response_bytes=16_384,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )

    with pytest.raises(OmniRouteTransportMalformed):
        transport.catalog()


def test_json_nesting_limit_is_explicit_and_parser_independent() -> None:
    def transport_for(depth: int) -> OmniRouteHTTPTransport:
        body = b"[" * depth + b"0" + b"]" * depth
        return OmniRouteHTTPTransport(
            "https://router.example.test",
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
        )

    assert transport_for(64).catalog()
    with pytest.raises(OmniRouteTransportMalformed, match="nesting"):
        transport_for(65).catalog()


def test_failure_exception_graph_does_not_retain_credentials_or_body() -> None:
    timeout_secret = "Authorization: Bearer timeout-secret"

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(timeout_secret, request=request)

    timed_out = OmniRouteHTTPTransport(
        "https://router.example.test",
        api_key="catalog-secret",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(timeout_handler),
    )
    with pytest.raises(OmniRouteTransportTimeout) as timeout_capture:
        timed_out.catalog()

    malformed_body = b'{"private":"response-body-secret"'
    malformed = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=malformed_body)),
    )
    with pytest.raises(OmniRouteTransportMalformed) as malformed_capture:
        malformed.catalog()

    content_length_secret = "content-length-secret"
    invalid_length = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-length": content_length_secret},
                content=b'{"data":[]}',
            )
        ),
    )
    with pytest.raises(OmniRouteTransportMalformed) as length_capture:
        invalid_length.catalog()

    transport_secret = "Authorization: Bearer transport-secret"

    def broken_transport(_: httpx.Request) -> httpx.Response:
        raise RuntimeError(transport_secret)

    broken = OmniRouteHTTPTransport(
        "https://router.example.test",
        allow_private_hosts=MOCK_ALLOWLIST,
        transport=httpx.MockTransport(broken_transport),
    )
    with pytest.raises(OmniRouteTransportError) as transport_capture:
        broken.catalog()

    for captured, secrets in (
        (timeout_capture.value, ("timeout-secret", "catalog-secret")),
        (malformed_capture.value, ("response-body-secret",)),
        (length_capture.value, (content_length_secret,)),
        (transport_capture.value, ("transport-secret",)),
    ):
        rendered = "".join(traceback.format_exception(captured))
        assert captured.__cause__ is None
        assert captured.__context__ is None
        assert all(secret not in rendered for secret in secrets)


def test_catalog_server_failure_is_unavailable_not_malformed() -> None:
    report = OmniRouteAvailabilityAdapter(
        OmniRouteHTTPTransport(
            "https://router.example.test",
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"error": "private"})),
        )
    ).evaluate(now=NOW)

    assert report.errors == ("catalog transport: unavailable",)


def test_runtime_unauthorized_and_malformed_fail_as_explicit_candidate_states() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "p/model", "provider": "p"}]})
        return httpx.Response(401, json={"error": "secret"})

    denied = OmniRouteAvailabilityAdapter(
        OmniRouteHTTPTransport(
            "https://router.example.test",
            management_token="management-secret",
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(unauthorized),
        )
    ).evaluate(now=NOW)

    assert denied.candidates[0].state is AvailabilityState.UNAUTHORIZED
    assert denied.errors == ("runtime transport: unauthorized",)

    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "p/model", "provider": "p"}]})
        return httpx.Response(200, content=b"not-json")

    broken = OmniRouteAvailabilityAdapter(
        OmniRouteHTTPTransport(
            "https://router.example.test",
            allow_private_hosts=MOCK_ALLOWLIST,
            transport=httpx.MockTransport(malformed),
        )
    ).evaluate(now=NOW)

    assert broken.candidates[0].state is AvailabilityState.MALFORMED
    assert broken.errors == ("runtime transport: malformed",)
