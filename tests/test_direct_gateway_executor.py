"""Tests for DirectGatewayExecutor (BOD-188 harness independence)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict.orchestration.contracts import AttemptUsage, RunEvent, WorkerTerminal
from verdict.orchestration.executors import DirectGatewayExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.tui import RunView, render_text


# --------------------------------------------------------------------------- helpers


class FakeTransport(httpx.AsyncBaseTransport):
    """Return a scripted httpx.Response for testing."""

    def __init__(
        self, status: int, body: dict[str, Any] | str, headers: dict[str, str] | None = None
    ) -> None:
        self._status = status
        self._body = json.dumps(body) if isinstance(body, dict) else body
        self._headers = headers or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=self._status,
            content=self._body.encode(),
            headers={**self._headers, "content-type": "application/json"},
        )


def _make_executor(transport: httpx.AsyncBaseTransport) -> DirectGatewayExecutor:
    """Build an executor wired to a fake transport (no real HTTP)."""
    exe = DirectGatewayExecutor(base_url="http://fake-gateway:20128", api_key="test-key")
    # Monkey-patch run to inject the transport into the httpx client
    original_run = exe.run

    async def patched_run(
        prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        import time as _time

        started = _time.monotonic()
        url = f"{exe.base_url}/v1/chat/completions"
        payload = {
            "model": route_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=exe._headers())
        duration = _time.monotonic() - started
        return DirectGatewayExecutor._interpret(resp, route_id=route_id, duration=duration)

    exe.run = patched_run  # type: ignore[assignment]
    return exe


def _ok_response(text: str = "Hello world", model: str = "cc/claude-sonnet-5") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


# --------------------------------------------------------------------------- text node tests


@pytest.mark.asyncio
async def test_success_text_node(tmp_path: Path) -> None:
    """Successful text response returns ok=True with text and usage."""
    transport = FakeTransport(200, _ok_response("Research result here"))
    exe = _make_executor(transport)
    result = await exe.run(
        "Summarize X", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert result.ok
    assert result.output == "Research result here"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.usage.tokens_source == "http_response"
    assert result.usage.turns == 1


@pytest.mark.asyncio
async def test_empty_output_rejected(tmp_path: Path) -> None:
    """Empty text content is classified as empty_output."""
    body = _ok_response("")
    transport = FakeTransport(200, body)
    exe = _make_executor(transport)
    result = await exe.run(
        "Summarize X", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert not result.ok
    assert "empty_output" in result.error


# --------------------------------------------------------------------------- diff tests


@pytest.mark.asyncio
async def test_diff_applies(tmp_path: Path) -> None:
    """A valid unified diff in the response text is accepted (text returned as-is)."""
    diff = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n"
    body = _ok_response(diff)
    transport = FakeTransport(200, body)
    exe = _make_executor(transport)
    result = await exe.run(
        "Implement changes", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert result.ok
    assert "hello.py" in result.output


# --------------------------------------------------------------------------- error classification


@pytest.mark.asyncio
async def test_429_rate_limit(tmp_path: Path) -> None:
    """429 is mapped to status_code=429 for recovery classification."""
    transport = FakeTransport(429, {"error": {"message": "rate limited"}}, {"retry-after": "30"})
    exe = _make_executor(transport)
    result = await exe.run(
        "Summarize", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert not result.ok
    assert result.status_code == 429
    assert result.retry_after_seconds == 30.0


@pytest.mark.asyncio
async def test_429_classified_same_as_prime(tmp_path: Path) -> None:
    """Recovery classifier treats a 429 WorkerTerminal identically for both executors."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    classifier = FailureIntelligence()

    # Direct gateway terminal
    gw_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error="rate limited",
        status_code=429,
        retry_after_seconds=30.0,
        duration_seconds=0.1,
    )
    gw_class = classifier.classify(gw_terminal, now=now)

    # Prime headless terminal (same shape)
    prime_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error="rate limited",
        status_code=429,
        duration_seconds=0.5,
    )
    prime_class = classifier.classify(prime_terminal, now=now)

    assert gw_class.category == prime_class.category
    assert gw_class.action == prime_class.action


@pytest.mark.asyncio
async def test_400_overflow_classified_same_as_prime(tmp_path: Path) -> None:
    """400 context-length overflow classified identically for both executors."""
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    classifier = FailureIntelligence()

    overflow_msg = "Input is too long for this model. Max input length is 200000 tokens."
    gw_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error=overflow_msg,
        status_code=400,
        duration_seconds=0.1,
    )
    prime_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error=overflow_msg,
        status_code=400,
        duration_seconds=0.5,
    )
    gw_class = classifier.classify(gw_terminal, now=now)
    prime_class = classifier.classify(prime_terminal, now=now)

    assert gw_class.category == prime_class.category
    assert gw_class.action == prime_class.action


@pytest.mark.asyncio
async def test_5xx_server_error(tmp_path: Path) -> None:
    """5xx is reflected as status_code for recovery to classify."""
    transport = FakeTransport(503, {"error": {"message": "upstream server error"}})
    exe = _make_executor(transport)
    result = await exe.run(
        "Summarize", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert not result.ok
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_malformed_response(tmp_path: Path) -> None:
    """Non-JSON 200 response is caught as malformed."""

    class RawTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200, content=b"not json at all", headers={"content-type": "text/plain"}
            )

    exe = _make_executor(RawTransport())
    result = await exe.run(
        "Summarize", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert not result.ok
    assert "malformed" in result.error


# --------------------------------------------------------------------------- usage recording


@pytest.mark.asyncio
async def test_usage_recorded(tmp_path: Path) -> None:
    """Usage is extracted from the standard OpenAI usage object."""
    body = _ok_response("text")
    body["usage"]["cost"] = 0.003
    transport = FakeTransport(200, body)
    exe = _make_executor(transport)
    result = await exe.run(
        "Summarize", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert result.ok
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.usage.cost_usd == 0.003


@pytest.mark.asyncio
async def test_no_usage_when_missing(tmp_path: Path) -> None:
    """Missing usage object returns usage=None, not an error."""
    body = _ok_response("text")
    del body["usage"]
    transport = FakeTransport(200, body)
    exe = _make_executor(transport)
    result = await exe.run(
        "Summarize", route_id="cc/claude-sonnet-5", cwd=tmp_path, timeout_seconds=30
    )
    assert result.ok
    assert result.usage is None


# --------------------------------------------------------------------------- RunView structural test


def _make_events(executor_label: str) -> list[dict[str, Any]]:
    """Build a minimal set of events that exercises every RunView section."""
    return [
        {
            "seq": 0,
            "at": "2026-09-28T12:00:00Z",
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "test goal", "controller_route": f"omniroute/{executor_label}"},
        },
        {
            "seq": 1,
            "at": "2026-09-28T12:00:01Z",
            "type": "understand",
            "node_id": "",
            "data": {"task_profile": "test", "risk": "low"},
        },
        {"seq": 2, "at": "2026-09-28T12:00:02Z", "type": "plan_started", "node_id": "", "data": {}},
        {
            "seq": 3,
            "at": "2026-09-28T12:00:03Z",
            "type": "plan_ready",
            "node_id": "",
            "data": {
                "topology": "linear",
                "rationale": ["simple task"],
                "nodes": [{"node_id": "node-1", "objective": "do X", "kind": "implement"}],
                "layers": [["node-1"]],
            },
        },
        {
            "seq": 4,
            "at": "2026-09-28T12:00:04Z",
            "type": "hydrate",
            "node_id": "",
            "data": {"context_sources": ["repo"], "byte_budget": 10000},
        },
        {
            "seq": 5,
            "at": "2026-09-28T12:00:05Z",
            "type": "topology",
            "node_id": "",
            "data": {"topology": "linear", "max_parallel": 1},
        },
        {
            "seq": 6,
            "at": "2026-09-28T12:00:06Z",
            "type": "eligibility",
            "node_id": "",
            "data": {"stats": {"discovered": 5, "selected": 1}},
        },
        {
            "seq": 7,
            "at": "2026-09-28T12:00:07Z",
            "type": "selection",
            "node_id": "node-1",
            "data": {"route": f"{executor_label}/model-a"},
        },
        {
            "seq": 8,
            "at": "2026-09-28T12:00:08Z",
            "type": "node_state",
            "node_id": "node-1",
            "data": {"state": "DISPATCHED"},
        },
        {
            "seq": 9,
            "at": "2026-09-28T12:00:09Z",
            "type": "dispatch",
            "node_id": "node-1",
            "data": {"route": f"{executor_label}/model-a", "attempt": 1},
        },
        {
            "seq": 10,
            "at": "2026-09-28T12:00:10Z",
            "type": "node_state",
            "node_id": "node-1",
            "data": {"state": "RUNNING"},
        },
        {
            "seq": 11,
            "at": "2026-09-28T12:00:15Z",
            "type": "terminal",
            "node_id": "node-1",
            "data": {"ok": True, "model": f"{executor_label}/model-a"},
        },
        {
            "seq": 12,
            "at": "2026-09-28T12:00:16Z",
            "type": "node_state",
            "node_id": "node-1",
            "data": {"state": "TERMINAL_SUCCESS"},
        },
        {
            "seq": 13,
            "at": "2026-09-28T12:00:17Z",
            "type": "verify",
            "node_id": "",
            "data": {"ok": True, "label": "lint", "detail": "passed"},
        },
        {
            "seq": 14,
            "at": "2026-09-28T12:00:18Z",
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE", "reason": "all nodes validated"},
        },
    ]


def test_run_view_same_sections_both_executors() -> None:
    """RunView renders the same section keys regardless of executor identity."""
    prime_events = _make_events("prime")
    gateway_events = _make_events("direct-gateway")

    prime_view = RunView.from_events(prime_events)
    gateway_view = RunView.from_events(gateway_events)

    # Both views should have the same structural attributes populated
    assert prime_view.goal and gateway_view.goal
    assert prime_view.understand and gateway_view.understand
    assert prime_view.layers and gateway_view.layers
    assert prime_view.outcome == gateway_view.outcome
    assert prime_view.event_count == gateway_view.event_count

    # The render function should produce the same section keys
    prime_text = render_text(prime_events)
    gateway_text = render_text(gateway_events)

    # Extract section headers (lines that are section titles)
    import re

    section_re = re.compile(r"^[A-Z][A-Z /]+$", re.MULTILINE)
    prime_sections = section_re.findall(prime_text)
    gateway_sections = section_re.findall(gateway_text)

    assert prime_sections == gateway_sections, (
        f"Section structure differs: {prime_sections} vs {gateway_sections}"
    )


PROOF_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "proof" / "harness-independence-2026-09-28"
)


def _load_events(run_dir: Path) -> list[dict]:
    events = []
    with open(run_dir / "events.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _load_receipt(run_dir: Path) -> dict:
    with open(run_dir / "receipt.json") as f:
        return json.load(f)


def test_proof_runs_same_view_sections() -> None:
    """Both committed proof runs render the same RunView section headers."""
    prime_dir = PROOF_DIR / "prime-run"
    gateway_dir = PROOF_DIR / "direct-gateway-run"

    assert prime_dir.exists(), f"Missing {prime_dir}"
    assert gateway_dir.exists(), f"Missing {gateway_dir}"

    prime_events = _load_events(prime_dir)
    gateway_events = _load_events(gateway_dir)

    prime_view = RunView.from_events(prime_events)
    gateway_view = RunView.from_events(gateway_events)

    # Both should have a goal, understand data, and events
    assert prime_view.goal, "prime run has no goal"
    assert gateway_view.goal, "gateway run has no goal"
    assert prime_view.event_count > 0
    assert gateway_view.event_count > 0

    # Render both and compare section headers
    prime_text = render_text(prime_events)
    gateway_text = render_text(gateway_events)

    section_re = re.compile(r"^[A-Z][A-Z /]+$", re.MULTILINE)
    prime_sections = section_re.findall(prime_text)
    gateway_sections = section_re.findall(gateway_text)

    assert prime_sections == gateway_sections, (
        f"Section structure differs:\nprime: {prime_sections}\ngateway: {gateway_sections}"
    )


def test_proof_receipts_same_schema() -> None:
    """Both committed receipts use the same schema."""
    prime_receipt = _load_receipt(PROOF_DIR / "prime-run")
    gateway_receipt = _load_receipt(PROOF_DIR / "direct-gateway-run")

    assert prime_receipt["schema"] == gateway_receipt["schema"]
    assert prime_receipt["schema"] == "verdict.run-receipt/v1"

    # Both should have standard receipt fields
    for receipt in [prime_receipt, gateway_receipt]:
        assert "run_id" in receipt
        assert "started_at" in receipt
        assert "topology" in receipt
        assert "events_digest" in receipt
