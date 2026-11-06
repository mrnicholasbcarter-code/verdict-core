"""The relay and the offload send verify the served route from OmniRoute headers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests.relay_proof_helpers import with_relay_proofs
from tests.test_proxy import FixedIntelligence
from verdict.free_tier_admit import execute_offload_chat
from verdict.models import RoutingDecision
from verdict.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy
from verdict.stream_terminal import identity_proof_status

SELECTED = "cc/claude-sonnet-4-6"
VERIFIED = {
    "x-omniroute-provider": "cc",
    "x-omniroute-model": "claude-sonnet-4-6",
    "x-omniroute-decision": "strategy=single; provider=cc; latency_ms=12",
    "x-omniroute-selected-connection-id": "conn-1",
}
MISMATCH = {**VERIFIED, "x-omniroute-model": "claude-haiku-4-5"}


class _SSE(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield SSE


SSE = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


def _proxy(headers: dict[str, str] | list[tuple[str, str]], *, body_model: str) -> UpstreamProxy:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        if payload.get("stream"):
            return httpx.Response(
                200,
                headers=[("content-type", "text/event-stream"), *_items(headers)],
                stream=_SSE(),
            )
        return httpx.Response(
            200, headers=_items(headers), json={"model": body_model, "choices": []}
        )

    return UpstreamProxy("http://upstream.test/v1", transport=httpx.MockTransport(handler))


def _items(headers: dict[str, str] | list[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(headers.items()) if isinstance(headers, dict) else headers


async def _chat(proxy: UpstreamProxy, model: str, *, stream: bool = False) -> Any:
    result = await proxy.chat(
        {"model": model, "messages": [{"role": "user", "content": "x"}], "stream": stream}
    )
    if isinstance(result, StreamedUpstreamResponse):
        async for _ in result.body:
            pass
    return result


async def test_verified_headers_attest_the_selected_route() -> None:
    result = await _chat(_proxy(VERIFIED, body_model="something-else"), SELECTED)
    assert isinstance(result, BufferedUpstreamResponse)
    route = result.actual_route
    assert route is not None
    assert route.model_id == SELECTED
    assert (route.gateway, route.provider, route.connection) == ("omniroute", "cc", "conn-1")
    assert route.endpoint == "omniroute-response-headers"


async def test_mismatched_headers_report_the_served_route() -> None:
    result = await _chat(_proxy(MISMATCH, body_model=SELECTED), SELECTED)
    assert result.actual_route is not None
    assert result.actual_route.model_id == "cc/claude-haiku-4-5"


async def test_combo_decision_on_concrete_route_is_still_a_mismatch() -> None:
    headers = {**VERIFIED, "x-omniroute-decision": "strategy=priority; provider=cc"}
    result = await _chat(_proxy(headers, body_model=SELECTED), SELECTED)
    assert result.actual_route is not None
    assert result.actual_route.model_id != SELECTED
    assert identity_proof_status(SELECTED, result.actual_route.model_id, attested=True) == (
        "identity_mismatch"
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"x-omniroute-provider": "cc"},
        [*VERIFIED.items(), ("x-omniroute-model", "claude-haiku-4-5")],
    ],
    ids=["missing_model", "conflicting_model"],
)
async def test_unknown_headers_are_unattested_and_skip_the_body(headers: Any) -> None:
    result = await _chat(_proxy(headers, body_model=SELECTED), SELECTED)
    assert result.actual_route is None


async def test_unqualified_selection_with_headers_is_unattested() -> None:
    result = await _chat(_proxy(VERIFIED, body_model="selected-model"), "selected-model")
    assert result.actual_route is None


async def test_opaque_selection_records_the_served_member() -> None:
    result = await _chat(_proxy(VERIFIED, body_model="auto/best"), "auto/best")
    assert result.actual_route is not None
    assert result.actual_route.model_id == "cc/claude-sonnet-4-6"
    status = identity_proof_status("auto/best", result.actual_route.model_id, attested=True)
    assert status == "opaque_alias"


async def test_opaque_selection_without_served_route_is_unattested() -> None:
    result = await _chat(_proxy({"x-omniroute-provider": "cc"}, body_model="x"), "auto/best")
    assert result.actual_route is None


async def test_no_identity_headers_keep_the_body_fallback() -> None:
    result = await _chat(_proxy({}, body_model="cc/claude-haiku-4-5"), SELECTED)
    assert result.actual_route is not None
    assert result.actual_route.gateway == "provider-response"
    assert result.actual_route.model_id == "cc/claude-haiku-4-5"


async def test_adapter_attestation_header_takes_precedence() -> None:
    adapter = {
        "gateway": "adapter",
        "provider": "cc",
        "connection": "a",
        "endpoint": "e",
        "protocol": "openai.chat",
        "model_id": SELECTED,
    }
    headers = {**MISMATCH, "x-verdict-actual-route": json.dumps(adapter)}
    result = await _chat(_proxy(headers, body_model="x"), SELECTED)
    assert result.actual_route is not None
    assert (result.actual_route.gateway, result.actual_route.model_id) == ("adapter", SELECTED)


async def test_stream_headers_verify_the_selected_route() -> None:
    result = await _chat(_proxy(VERIFIED, body_model="x"), SELECTED, stream=True)
    assert isinstance(result, StreamedUpstreamResponse)
    assert result.actual_route is not None
    assert result.actual_route.model_id == SELECTED


async def test_stream_headers_report_a_mismatch() -> None:
    result = await _chat(_proxy(MISMATCH, body_model="x"), SELECTED, stream=True)
    assert isinstance(result, StreamedUpstreamResponse)
    assert result.actual_route is not None
    assert result.actual_route.model_id == "cc/claude-haiku-4-5"


async def test_stream_without_identity_headers_stays_unattested() -> None:
    result = await _chat(_proxy({}, body_model="x"), SELECTED, stream=True)
    assert result.actual_route is None


def _offload(headers: dict[str, str]) -> tuple[str, str]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            json={
                "model": SELECTED,
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
            },
        )

    return execute_offload_chat(
        "http://omniroute.test", SELECTED, "task", transport=httpx.MockTransport(handler)
    )


def test_offload_header_mismatch_is_an_error() -> None:
    outcome, detail = _offload(MISMATCH)
    assert outcome == "error"
    assert detail == f"identity mismatch: selected {SELECTED!r}, served 'cc/claude-haiku-4-5'"


def test_offload_verified_headers_are_sent() -> None:
    assert _offload(VERIFIED) == ("sent", "done")


def test_offload_without_identity_headers_relies_on_the_body_check() -> None:
    assert _offload({}) == ("sent", "done")


@pytest.mark.parametrize("headers", [VERIFIED, MISMATCH], ids=["verified", "mismatch"])
def test_relay_header_mismatch_fails_the_attempt_closed(monkeypatch, headers) -> None:
    class ConcreteIntelligence(FixedIntelligence):
        primary_model = SELECTED

        async def route(
            self,
            task: str,
            criticality: str = "medium",
            context: dict[str, Any] | None = None,
            *,
            request_id: str | None = None,
        ) -> RoutingDecision:
            return with_relay_proofs(
                RoutingDecision(
                    model=SELECTED,
                    provider="omniroute",
                    tier=2,
                    reason="test selection",
                    request_id="request-1",
                    managed_backend_status="healthy",
                    quality_outcome="unknown",
                ),
                context,
            )

    monkeypatch.setattr(api, "_build_intelligence", lambda: ConcreteIntelligence())
    monkeypatch.setattr(api, "_build_proxy", lambda: _proxy(headers, body_model=SELECTED))
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "correlation_id": "header-mismatch",
            },
        )
        evidence = client.get("/v1/route/explain?correlation_id=header-mismatch").json()

    rendered = json.dumps(evidence)
    if headers is VERIFIED:
        assert response.status_code == 200
        assert evidence["outcome_event"]["outcome"] == "success"
        assert "identity_mismatch" not in rendered
    else:
        assert response.status_code == 502
        assert evidence["outcome_event"]["outcome"] != "success"
        assert "identity_mismatch" in rendered
