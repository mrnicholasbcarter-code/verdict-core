from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from verdict.workers import (
    OmniRouteWorkerClient,
    WorkerPool,
    WorkerRequest,
    WorkerUnavailable,
)


def _transport(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _request(task_id: str = "task-1", model: str = "auto/best-free") -> WorkerRequest:
    return WorkerRequest(
        task_id=task_id,
        model=model,
        messages=[{"role": "user", "content": "reply with OK"}],
        max_tokens=1,
    )


@pytest.mark.asyncio
async def test_worker_discovers_live_catalog_and_forwards_selected_model() -> None:
    requests: list[tuple[str, dict[str, Any] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode()) if request.method == "POST" else None
        requests.append((request.url.path, body))
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "auto/best-free", "owned_by": "omniroute"},
                        {"id": "openrouter/test-model:free", "owned_by": "openrouter"},
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    client = OmniRouteWorkerClient(
        "http://router.test/v1", transport=_transport(handler), retry_backoff_seconds=0
    )
    result = await client.complete(_request())

    assert result.ok
    assert result.selected_model == "auto/best-free"
    assert requests[0][0] == "/v1/models"
    assert requests[1] == (
        "/v1/chat/completions",
        {
            "model": "auto/best-free",
            "messages": _request().messages,
            "stream": False,
            "max_tokens": 1,
        },
    )


@pytest.mark.asyncio
async def test_worker_fails_over_to_another_live_model_on_transient_error() -> None:
    selected: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "auto/best-free"},
                        {"id": "provider/fallback:free"},
                    ]
                },
            )
        payload = json.loads((await request.aread()).decode())
        selected.append(payload["model"])
        return httpx.Response(503 if len(selected) == 1 else 200, json={"error": "temporary"})

    client = OmniRouteWorkerClient(
        "http://router.test/v1",
        max_attempts=2,
        transport=_transport(handler),
        retry_backoff_seconds=0,
    )
    result = await client.complete(_request())

    assert result.ok
    assert result.attempts == 2
    assert selected == ["auto/best-free", "provider/fallback:free"]


@pytest.mark.asyncio
async def test_worker_does_not_retry_non_transient_client_errors() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "auto/best-free"}]})
        calls += 1
        return httpx.Response(400, json={"error": "invalid request"})

    client = OmniRouteWorkerClient(
        "http://router.test/v1", transport=_transport(handler), retry_backoff_seconds=0
    )
    with pytest.raises(WorkerUnavailable, match="HTTP 400"):
        await client.complete(_request())
    assert calls == 1


@pytest.mark.asyncio
async def test_worker_retries_same_model_when_no_alternate_is_advertised() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "auto/best-free"}]})
        calls += 1
        return httpx.Response(503 if calls < 3 else 200, json={"error": "temporary"})

    client = OmniRouteWorkerClient(
        "http://router.test/v1",
        max_attempts=3,
        transport=_transport(handler),
        retry_backoff_seconds=0,
    )
    result = await client.complete(_request())

    assert result.ok
    assert result.attempts == 3
    assert calls == 3


@pytest.mark.asyncio
async def test_worker_pool_respects_concurrency_and_preserves_order() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "auto/best-free"}]})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, json={"choices": []})

    client = OmniRouteWorkerClient(
        "http://router.test/v1", transport=_transport(handler), retry_backoff_seconds=0
    )
    results = await WorkerPool(client, max_concurrency=2).run(
        [_request(f"task-{index}") for index in range(6)]
    )

    assert peak <= 2
    assert [item.task_id for item in results] == [f"task-{index}" for index in range(6)]
    assert all(item.ok for item in results)


@pytest.mark.asyncio
async def test_explicit_provider_model_must_be_present_in_live_catalog() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openrouter/test:free"}]})
        return httpx.Response(200, json={"choices": []})

    client = OmniRouteWorkerClient(
        "http://router.test/v1", transport=_transport(handler), retry_backoff_seconds=0
    )
    with pytest.raises(WorkerUnavailable, match="no OmniRoute model"):
        await client.complete(_request(model="openrouter/missing:free"))
