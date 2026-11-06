"""The direct-gateway worker checks which route OmniRoute actually served."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_orch_runtime import Executor, make, node
from verdict.orchestration.contracts import RunOutcome, WorkerTerminal, WorkGraph
from verdict.orchestration.executors import DirectGatewayExecutor
from verdict.orchestration.recovery import FailureIntelligence

ROUTE = "cc/claude-sonnet-5"
IDENTITY = {
    "X-OmniRoute-Provider": "cc",
    "X-OmniRoute-Model": "claude-sonnet-5",
    "X-OmniRoute-Decision": "strategy=single; provider=cc; latency_ms=12",
    "X-OmniRoute-Selected-Connection-Id": "conn-1",
}
TEXT_PROMPT = "NODE: research-1\nOBJECTIVE: answer\n"
IMPLEMENT_PROMPT = "NODE: impl-1\nOBJECTIVE: edit\nOWNED_FILES: owned.py\n"


def _body(text: str = "Hello", model: str | None = ROUTE) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    if model is not None:
        body["model"] = model
    return body


def _sse(body: dict[str, Any]) -> bytes:
    chunk = {
        "id": body["id"],
        "model": body.get("model", ""),
        "choices": [{"delta": {"content": body["choices"][0]["message"]["content"]}}],
    }
    end = {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": body["usage"]}
    events = [json.dumps(chunk), json.dumps(end), "[DONE]"]
    return "".join(f"data: {event}\n\n" for event in events).encode()


def _gateway(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], body: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["stream"]:
            sse = {**headers, "content-type": "text/event-stream"}
            return httpx.Response(200, headers=sse, content=_sse(body))
        return httpx.Response(200, headers=headers, json=body)

    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


async def _run(prompt: str, cwd: Path) -> WorkerTerminal:
    return await DirectGatewayExecutor(base_url="http://gateway.test", api_key="k").run(
        prompt, route_id=ROUTE, cwd=cwd, timeout_seconds=5
    )


async def test_verified_non_streamed_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _gateway(monkeypatch, IDENTITY, _body())
    terminal = await _run(TEXT_PROMPT, tmp_path)
    assert terminal.ok, terminal.error
    assert terminal.model == ROUTE
    assert terminal.served_check is not None
    assert terminal.served_check["status"] == "verified"
    assert terminal.served_check["served"]["connection_id"] == "conn-1"


async def test_verified_streamed_answer_keeps_gateway_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "owned.py").write_text("x = 1\n")
    _gateway(monkeypatch, IDENTITY, _body("FILE: owned.py\n```\nx = 2\n```\nRESULT: DONE"))
    terminal = await _run(IMPLEMENT_PROMPT, tmp_path)
    assert terminal.ok, terminal.error
    assert (tmp_path / "owned.py").read_text() == "x = 2\n"
    assert terminal.served_check is not None
    assert terminal.served_check["status"] == "verified"
    assert terminal.served_check["served_route"] == ROUTE


async def test_streamed_rejected_edit_keeps_served_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _gateway(monkeypatch, IDENTITY, _body("FILE: other.py\n```\nx = 2\n```\nRESULT: DONE"))
    terminal = await _run(IMPLEMENT_PROMPT, tmp_path)
    assert not terminal.ok
    assert terminal.error.startswith("diff_rejected")
    assert terminal.served_check is not None
    assert terminal.served_check["status"] == "verified"


async def test_served_mismatch_fails_the_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = {**IDENTITY, "X-OmniRoute-Model": "claude-haiku-5"}
    _gateway(monkeypatch, served, _body())
    terminal = await _run(TEXT_PROMPT, tmp_path)
    assert not terminal.ok
    assert terminal.error == "model_mismatch"
    assert terminal.model == "cc/claude-haiku-5"
    assert terminal.output == "Hello"
    assert terminal.usage is not None
    assert terminal.served_check is not None
    assert terminal.served_check["status"] == "mismatch"
    failure = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (failure.category, failure.action, failure.scope) == (
        "model_mismatch",
        "REROUTE",
        "route",
    )


async def test_missing_identity_is_recorded_unknown_not_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _gateway(monkeypatch, {}, _body(model=None))
    terminal = await _run(TEXT_PROMPT, tmp_path)
    assert terminal.ok, terminal.error
    assert terminal.model == ""
    assert terminal.served_check is not None
    assert terminal.served_check["status"] == "unknown"
    assert terminal.served_check["reasons"] == ["no_served_identity_headers"]


def test_error_response_records_served_check_only_with_identity() -> None:
    request = httpx.Request("POST", "http://gateway.test/v1/chat/completions")
    plain = httpx.Response(503, text="down", request=request)
    served = httpx.Response(503, headers=IDENTITY, text="down", request=request)
    kwargs: dict[str, Any] = {"route_id": ROUTE, "duration": 0.1}
    assert DirectGatewayExecutor._interpret(plain, **kwargs).served_check is None
    check = DirectGatewayExecutor._interpret(served, **kwargs).served_check
    assert check is not None and check["status"] == "verified"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


CHECK = {"status": "verified", "selected_route": "cc/s", "served_route": "cc/s"}


class ServedExecutor(Executor):
    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        terminal = await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )
        if prompt.startswith("a:"):
            return replace(terminal, served_check=CHECK)
        return terminal


async def test_runtime_records_served_check_only_when_present(repo: Path) -> None:
    graph = WorkGraph("g", (node("a"), node("b")))
    runtime, events, _ = make(repo, graph, ServedExecutor({}, delay=0), ["cc/s"])
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    (with_check,) = events.of("terminal", "a")
    (without_check,) = events.of("terminal", "b")
    assert with_check["served_check"] == CHECK
    assert "served_check" not in without_check
    attempts = runtime.run_dir / "attempts"
    assert json.loads((attempts / "a-a1.json").read_text())["served_check"] == CHECK
    assert "served_check" not in json.loads((attempts / "b-a1.json").read_text())
