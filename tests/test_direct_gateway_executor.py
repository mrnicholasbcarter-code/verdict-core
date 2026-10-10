"""Tests for DirectGatewayExecutor (BOD-188 harness independence)."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from verdict.orchestration.contracts import WorkerTerminal
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


def _make_executor(transport: FakeTransport) -> DirectGatewayExecutor:
    """Build an executor wired to a fake transport, routing through full run()."""
    exe = DirectGatewayExecutor(base_url="http://fake-gateway:20128", api_key="test-key")
    # Patch httpx.AsyncClient to inject our transport
    _orig_init = httpx.AsyncClient.__init__

    def _patched_init(self_client: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        _orig_init(self_client, **kwargs)

    exe._patched_client_init = _patched_init  # type: ignore[attr-defined]
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


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True
    )
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


def _text_node_prompt(text: str = "Summarize the codebase") -> str:
    """A prompt WITHOUT OWNED_FILES (text-only / research node)."""
    return f"GOAL: test\n\nNODE: node-1\nOBJECTIVE: summarize\nOWNED_FILES: (none)\n\n{text}"


def _implement_prompt(owned_files: list[str]) -> str:
    """A prompt WITH OWNED_FILES (implement node), mimicking hydrate_node_prompt."""
    files_str = ", ".join(owned_files)
    return (
        f"GOAL: test goal\n\n"
        f"NODE: impl-1\n"
        f"OBJECTIVE: implement feature\n"
        f"OWNED_FILES: {files_str}\n"
        f"VERIFICATION_COMMAND: python -m pytest\n\n"
        f"RULES:\n"
        f"  - Only edit files listed in OWNED_FILES.\n"
        f"  - Finish with RESULT: DONE\n"
    )


async def _run_with_transport(
    exe: DirectGatewayExecutor,
    transport: FakeTransport,
    prompt: str,
    route_id: str,
    cwd: Path,
    timeout: float = 30,
) -> WorkerTerminal:
    """Run executor.run() using a fake transport instead of real HTTP."""
    with patch("httpx.AsyncClient") as mock_cls:
        # Make httpx.AsyncClient return a context manager that uses our transport
        mock_client = AsyncMock()

        async def fake_post(url: str, json: Any = None, headers: Any = None) -> httpx.Response:
            req = httpx.Request("POST", url, json=json, headers=headers or {})
            return await transport.handle_async_request(req)

        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        terminal = await exe.run(prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout)
        assert terminal.executor_kind == "live"
        return terminal


# --------------------------------------------------------------------------- text node tests


@pytest.mark.asyncio
async def test_success_text_node(tmp_path: Path) -> None:
    """Successful text response returns ok=True with text and usage."""
    transport = FakeTransport(200, _ok_response("Hello world"))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert result.ok
    assert "Hello" in result.output
    assert result.usage is not None
    assert result.usage.input_tokens == 10


@pytest.mark.asyncio
async def test_empty_output_rejected(tmp_path: Path) -> None:
    """Empty model output returns ok=False."""
    transport = FakeTransport(200, _ok_response("   "))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert not result.ok
    assert "empty_output" in result.error


# --------------------------------------------------------------------------- diff apply tests


@pytest.mark.asyncio
async def test_diff_applies_and_changes_file(tmp_path: Path) -> None:
    """Valid diff for an owned file is applied and changes the file on disk."""
    _init_git_repo(tmp_path)
    (tmp_path / "hello.py").write_text("print('old')\n")
    subprocess.run(["git", "add", "hello.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add hello"], cwd=str(tmp_path), check=True, capture_output=True
    )

    diff_text = (
        "```diff\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
        "```\n\n"
        "RESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(diff_text))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    prompt = _implement_prompt(["hello.py"])
    result = await _run_with_transport(exe, transport, prompt, "cc/claude-sonnet-5", tmp_path)

    assert result.ok, f"Expected ok but got error: {result.error}"
    # Verify the file was actually changed on disk
    content = (tmp_path / "hello.py").read_text()
    assert "print('new')" in content
    assert "print('old')" not in content


@pytest.mark.asyncio
async def test_diff_non_owned_file_rejected(tmp_path: Path) -> None:
    """Diff touching a file NOT in owned_files is rejected with no disk changes."""
    _init_git_repo(tmp_path)
    (tmp_path / "hello.py").write_text("print('old')\n")
    (tmp_path / "secret.py").write_text("secret = True\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init files"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # Diff tries to modify secret.py but only hello.py is owned
    diff_text = (
        "```diff\n"
        "--- a/secret.py\n"
        "+++ b/secret.py\n"
        "@@ -1 +1 @@\n"
        "-secret = True\n"
        "+secret = False\n"
        "```\n\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(diff_text))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    prompt = _implement_prompt(["hello.py"])
    result = await _run_with_transport(exe, transport, prompt, "cc/claude-sonnet-5", tmp_path)

    assert not result.ok
    assert "diff_rejected" in result.error
    assert "outside owned_files" in result.error
    # secret.py must NOT be changed
    assert (tmp_path / "secret.py").read_text() == "secret = True\n"


@pytest.mark.asyncio
async def test_path_traversal_rejected(tmp_path: Path) -> None:
    """Diff with path traversal (.. component) is rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "hello.py").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True
    )

    diff_text = (
        "```diff\n"
        "--- a/../../../etc/passwd\n"
        "+++ b/../../../etc/passwd\n"
        "@@ -1 +1 @@\n"
        "-root:x\n"
        "+hacked\n"
        "```\n\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(diff_text))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    prompt = _implement_prompt(["hello.py"])
    result = await _run_with_transport(exe, transport, prompt, "cc/claude-sonnet-5", tmp_path)

    assert not result.ok
    assert "diff_rejected" in result.error
    assert "path traversal" in result.error or "invalid path" in result.error


@pytest.mark.asyncio
async def test_malformed_diff_rejected(tmp_path: Path) -> None:
    """Model output with no valid diff returns diff_apply_failed or diff_rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "hello.py").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # No diff at all in the output
    transport = FakeTransport(200, _ok_response("I made some changes to hello.py\nRESULT: DONE"))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    prompt = _implement_prompt(["hello.py"])
    result = await _run_with_transport(exe, transport, prompt, "cc/claude-sonnet-5", tmp_path)

    assert not result.ok
    assert "diff_rejected" in result.error
    assert "no unified diff" in result.error


@pytest.mark.asyncio
async def test_fenced_and_unfenced_diff_extraction(tmp_path: Path) -> None:
    """Both fenced (```diff) and unfenced raw diffs are extracted."""
    _init_git_repo(tmp_path)
    (tmp_path / "hello.py").write_text("print('old')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # Unfenced diff (raw)
    raw_diff = (
        "Here are my changes:\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
        "\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(raw_diff))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    prompt = _implement_prompt(["hello.py"])
    result = await _run_with_transport(exe, transport, prompt, "cc/claude-sonnet-5", tmp_path)

    assert result.ok, f"Unfenced diff should apply, got: {result.error}"
    assert "print('new')" in (tmp_path / "hello.py").read_text()


@pytest.mark.asyncio
async def test_absolute_path_rejected(tmp_path: Path) -> None:
    """Diff with absolute paths is rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "hello.py").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True
    )

    diff_text = "```diff\n--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1 +1 @@\n-root\n+hacked\n```"
    transport = FakeTransport(200, _ok_response(diff_text))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    prompt = _implement_prompt(["hello.py"])
    result = await _run_with_transport(exe, transport, prompt, "cc/claude-sonnet-5", tmp_path)

    assert not result.ok
    assert "diff_rejected" in result.error
    assert "absolute path" in result.error or "invalid path" in result.error


# --------------------------------------------------------------------------- error classification


@pytest.mark.asyncio
async def test_429_rate_limit(tmp_path: Path) -> None:
    """429 is mapped to status_code=429 for recovery classification."""
    transport = FakeTransport(429, {"error": {"message": "rate limited"}}, {"retry-after": "30"})
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert not result.ok
    assert result.status_code == 429
    assert result.retry_after_seconds == 30.0


@pytest.mark.asyncio
async def test_429_classified_same_as_prime(tmp_path: Path) -> None:
    """FailureIntelligence classifies a 429 from DirectGateway same as PrimeHeadless."""
    classifier = FailureIntelligence()
    # Direct-gateway 429
    gw_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error="rate limited",
        status_code=429,
        retry_after_seconds=30.0,
    )
    gw_fc = classifier.classify(gw_terminal, now=datetime.now(timezone.utc))
    # Prime-headless 429 (same shape)
    prime_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error="[429] rate limited",
        status_code=429,
        retry_after_seconds=30.0,
    )
    prime_fc = classifier.classify(prime_terminal, now=datetime.now(timezone.utc))
    assert gw_fc.category == prime_fc.category, (
        f"Gateway: {gw_fc.category}, Prime: {prime_fc.category}"
    )
    assert gw_fc.action == prime_fc.action


@pytest.mark.asyncio
async def test_400_overflow_classified_same_as_prime(tmp_path: Path) -> None:
    """A 400 context-length error from DirectGateway classifies as input_too_long."""
    classifier = FailureIntelligence()
    gw_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error="prompt is too long: 300000 tokens > 200000 maximum",
        status_code=400,
    )
    gw_fc = classifier.classify(gw_terminal, now=datetime.now(timezone.utc))
    prime_terminal = WorkerTerminal(
        ok=False,
        model="cc/claude-sonnet-5",
        error="[400] prompt is too long: 300000 tokens > 200000 maximum",
        status_code=400,
    )
    prime_fc = classifier.classify(prime_terminal, now=datetime.now(timezone.utc))
    assert gw_fc.category == prime_fc.category


@pytest.mark.asyncio
async def test_5xx_server_error(tmp_path: Path) -> None:
    """5xx responses produce ok=False with status code."""
    transport = FakeTransport(502, {"error": {"message": "bad gateway"}})
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert not result.ok
    assert result.status_code == 502


@pytest.mark.asyncio
async def test_malformed_response(tmp_path: Path) -> None:
    """Non-JSON response produces ok=False with malformed error."""
    transport = FakeTransport(200, "not json at all")
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert not result.ok
    assert "malformed" in result.error


# --------------------------------------------------------------------------- usage


@pytest.mark.asyncio
async def test_usage_recorded(tmp_path: Path) -> None:
    """Token usage from the response is recorded in the WorkerTerminal."""
    body = _ok_response("text")
    body["usage"] = {"prompt_tokens": 100, "completion_tokens": 200, "cost": 0.01}
    transport = FakeTransport(200, body)
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert result.ok
    assert result.usage is not None
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 200
    assert result.usage.cost_usd == 0.01


@pytest.mark.asyncio
async def test_no_usage_when_missing(tmp_path: Path) -> None:
    """Missing usage object returns usage=None, not an error."""
    body = _ok_response("text")
    del body["usage"]
    transport = FakeTransport(200, body)
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _text_node_prompt(), "cc/claude-sonnet-5", tmp_path
    )
    assert result.ok
    assert result.usage is None


# --------------------------------------------------------------------------- unit tests for static methods


def test_parse_owned_files_basic() -> None:
    prompt = "OWNED_FILES: src/main.py, tests/test_main.py"
    assert DirectGatewayExecutor._parse_owned_files(prompt) == ["src/main.py", "tests/test_main.py"]


def test_parse_owned_files_none() -> None:
    prompt = "OWNED_FILES: (none)"
    assert DirectGatewayExecutor._parse_owned_files(prompt) == []


def test_parse_owned_files_missing() -> None:
    prompt = "Just do something"
    assert DirectGatewayExecutor._parse_owned_files(prompt) == []


def test_extract_diff_fenced() -> None:
    text = "Here:\n```diff\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n```\nDone."
    diff = DirectGatewayExecutor._extract_diff(text)
    assert diff is not None
    assert "--- a/f.py" in diff


def test_extract_diff_unfenced() -> None:
    text = "Changes:\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new"
    diff = DirectGatewayExecutor._extract_diff(text)
    assert diff is not None
    assert "--- a/f.py" in diff


def test_extract_diff_none() -> None:
    text = "I made changes to the file. RESULT: DONE"
    assert DirectGatewayExecutor._extract_diff(text) is None


def test_validate_diff_paths_ok() -> None:
    diff = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-old\n+new"
    assert DirectGatewayExecutor._validate_diff_paths(diff, ["hello.py"]) is None


def test_validate_diff_paths_outside() -> None:
    diff = "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-old\n+new"
    err = DirectGatewayExecutor._validate_diff_paths(diff, ["hello.py"])
    assert err is not None
    assert "outside owned_files" in err


def test_validate_diff_paths_traversal() -> None:
    diff = "--- a/../../../etc/passwd\n+++ b/../../../etc/passwd\n@@ -1 +1 @@\n-x\n+y"
    err = DirectGatewayExecutor._validate_diff_paths(diff, ["hello.py"])
    assert err is not None
    assert "path traversal" in err


def test_validate_diff_paths_absolute() -> None:
    diff = "--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1 +1 @@\n-x\n+y"
    err = DirectGatewayExecutor._validate_diff_paths(diff, ["hello.py"])
    assert err is not None
    assert "absolute path" in err


def test_validate_diff_paths_dev_null_allowed() -> None:
    """New file creation uses /dev/null, which should be allowed."""
    diff = "--- /dev/null\n+++ b/hello.py\n@@ -0,0 +1 @@\n+new content"
    assert DirectGatewayExecutor._validate_diff_paths(diff, ["hello.py"]) is None


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

    assert prime_view.goal and gateway_view.goal
    assert prime_view.understand and gateway_view.understand
    assert prime_view.layers and gateway_view.layers
    assert prime_view.outcome == gateway_view.outcome
    assert prime_view.event_count == gateway_view.event_count

    prime_text = render_text(prime_events)
    gateway_text = render_text(gateway_events)

    section_re = re.compile(r"^[A-Z][A-Z /]+$", re.MULTILINE)
    prime_sections = section_re.findall(prime_text)
    gateway_sections = section_re.findall(gateway_text)

    assert prime_sections == gateway_sections, (
        f"Section structure differs: {prime_sections} vs {gateway_sections}"
    )


# --------------------------------------------------------------------------- proof loading


PROOF_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "proof" / "harness-independence-2026-09-28"
)


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with open(run_dir / "events.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _load_receipt(run_dir: Path) -> dict[str, Any]:
    with open(run_dir / "receipt.json") as f:
        return json.load(f)


def test_proof_runs_same_view_sections() -> None:
    """Both committed proof runs render the same RunView section headers."""
    prime_dir = PROOF_DIR / "prime-run"
    gateway_dir = PROOF_DIR / "direct-gateway-run"

    if not prime_dir.exists() or not gateway_dir.exists():
        pytest.skip("Proof artifacts not yet committed")

    prime_events = _load_events(prime_dir)
    gateway_events = _load_events(gateway_dir)

    prime_view = RunView.from_events(prime_events)
    gateway_view = RunView.from_events(gateway_events)

    assert prime_view.goal, "prime run has no goal"
    assert gateway_view.goal, "gateway run has no goal"
    assert prime_view.event_count > 0
    assert gateway_view.event_count > 0

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
    prime_dir = PROOF_DIR / "prime-run"
    gateway_dir = PROOF_DIR / "direct-gateway-run"

    if not prime_dir.exists() or not gateway_dir.exists():
        pytest.skip("Proof artifacts not yet committed")

    prime_receipt = _load_receipt(prime_dir)
    gateway_receipt = _load_receipt(gateway_dir)

    assert prime_receipt["schema"] == gateway_receipt["schema"]
    assert prime_receipt["schema"] == "verdict.run-receipt/v1"

    for receipt in [prime_receipt, gateway_receipt]:
        assert "run_id" in receipt
        assert "started_at" in receipt
        assert "topology" in receipt
        assert "events_digest" in receipt


def test_proof_gateway_run_has_executor_identity() -> None:
    """Direct-gateway proof run has executor identity in run_started event."""
    gateway_dir = PROOF_DIR / "direct-gateway-run"
    if not gateway_dir.exists():
        pytest.skip("Proof artifacts not yet committed")

    events = _load_events(gateway_dir)
    run_started = [e for e in events if e.get("type") == "run_started"]
    assert run_started, "no run_started event"
    data = run_started[0].get("data", {})
    assert "executor" in data, "run_started event missing executor field"
    assert data["executor"] == "direct-gateway"


def test_proof_runs_both_complete() -> None:
    """Both committed proof runs have COMPLETE outcome."""
    prime_dir = PROOF_DIR / "prime-run"
    gateway_dir = PROOF_DIR / "direct-gateway-run"

    if not prime_dir.exists() or not gateway_dir.exists():
        pytest.skip("Proof artifacts not yet committed")

    for run_dir in [prime_dir, gateway_dir]:
        events = _load_events(run_dir)
        finished = [e for e in events if e.get("type") == "run_finished"]
        assert finished, f"no run_finished in {run_dir.name}"
        assert finished[-1]["data"]["outcome"] == "COMPLETE", (
            f"{run_dir.name} outcome: {finished[-1]['data']['outcome']}"
        )


# --------------------------------------------------------------------------- adversarial security tests
# Verify that DirectGatewayExecutor rejects dangerous diff operations:
# rename, copy, mode-only, symlink, binary, deletion, path escape.


@pytest.mark.asyncio
async def test_security_rename_rejected(tmp_path: Path) -> None:
    """Rename-only diff must be rejected — not silently applied."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    rename_response = (
        "```diff\n"
        "diff --git a/owned.py b/injected.py\n"
        "similarity index 100%\n"
        "rename from owned.py\n"
        "rename to injected.py\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(rename_response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "rename diff must be rejected"
    assert "diff_rejected" in (result.error or "")
    assert not (tmp_path / "injected.py").exists(), "rename must NOT be applied"
    assert (tmp_path / "owned.py").exists(), "owned.py must still exist"


@pytest.mark.asyncio
async def test_security_copy_rejected(tmp_path: Path) -> None:
    """Copy-only diff must be rejected — not silently applied."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    copy_response = (
        "```diff\n"
        "diff --git a/owned.py b/evil.py\n"
        "similarity index 100%\n"
        "copy from owned.py\n"
        "copy to evil.py\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(copy_response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "copy diff must be rejected"
    assert "diff_rejected" in (result.error or "")
    assert not (tmp_path / "evil.py").exists(), "copy must NOT be applied"


@pytest.mark.asyncio
async def test_security_mode_only_rejected(tmp_path: Path) -> None:
    """Mode-only diff (chmod) must be rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    mode_response = (
        "```diff\n"
        "diff --git a/owned.py b/owned.py\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(mode_response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "mode-only diff must be rejected"
    assert "diff_rejected" in (result.error or "")


@pytest.mark.asyncio
async def test_security_symlink_rejected(tmp_path: Path) -> None:
    """Symlink creation diff (mode 120000) must be rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    symlink_response = (
        "```diff\n"
        "diff --git a/link.py b/link.py\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/link.py\n"
        "@@ -0,0 +1 @@\n"
        "+/etc/passwd\n"
        "\\ No newline at end of file\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(symlink_response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "symlink creation must be rejected"
    assert "diff_rejected" in (result.error or "")


@pytest.mark.asyncio
async def test_security_binary_rejected(tmp_path: Path) -> None:
    """Binary patch must be rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    binary_response = (
        "```diff\n"
        "diff --git a/owned.py b/owned.py\n"
        "GIT binary patch\n"
        "literal 4\n"
        "Lc${Nk0000400IC2\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(binary_response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "binary patch must be rejected"
    assert "diff_rejected" in (result.error or "")


@pytest.mark.asyncio
async def test_security_deletion_rejected(tmp_path: Path) -> None:
    """File deletion diff must be rejected (current nodes don't allow deletions)."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    delete_response = (
        "```diff\n"
        "diff --git a/owned.py b/owned.py\n"
        "deleted file mode 100644\n"
        "--- a/owned.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-print('hello')\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(delete_response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "deletion must be rejected"
    assert "diff_rejected" in (result.error or "")
    assert (tmp_path / "owned.py").exists(), "owned.py must NOT be deleted"


@pytest.mark.asyncio
async def test_security_src_owned_dst_not_owned_rejected(tmp_path: Path) -> None:
    """Diff with --- owned but +++ not-owned must be rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # Rename with modification: owned.py -> evil.py
    response = (
        "```diff\n"
        "diff --git a/owned.py b/evil.py\n"
        "similarity index 50%\n"
        "rename from owned.py\n"
        "rename to evil.py\n"
        "--- a/owned.py\n"
        "+++ b/evil.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "rename-with-modify to unowned path must be rejected"
    assert "diff_rejected" in (result.error or "")


@pytest.mark.asyncio
async def test_security_diff_header_path_mismatch_rejected(tmp_path: Path) -> None:
    """diff --git a/x b/y where y is not owned must be rejected (even with matching +++ line)."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # Copy with content: copies from owned to unowned
    response = (
        "```diff\n"
        "diff --git a/owned.py b/unowned.py\n"
        "similarity index 50%\n"
        "copy from owned.py\n"
        "copy to unowned.py\n"
        "--- a/owned.py\n"
        "+++ b/unowned.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "copy to unowned path must be rejected"
    assert "diff_rejected" in (result.error or "")


@pytest.mark.asyncio
async def test_security_dotslash_double_slash_normalisation(tmp_path: Path) -> None:
    """Paths with ./ or // that normalise outside owned_files must be rejected."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    # Try to create a file via sneaky path normalisation
    response = (
        "```diff\n"
        "diff --git a/./owned.py b/./subdir/../evil.py\n"
        "--- a/./owned.py\n"
        "+++ b/./subdir/../evil.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+pwned\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert not result.ok, "normalised path outside owned_files must be rejected"
    assert "diff_rejected" in (result.error or "")


@pytest.mark.asyncio
async def test_security_legitimate_diff_still_applies(tmp_path: Path) -> None:
    """A normal modify-only diff of owned files must still succeed."""
    _init_git_repo(tmp_path)
    (tmp_path / "owned.py").write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add"], cwd=str(tmp_path), check=True, capture_output=True
    )

    response = (
        "```diff\n"
        "diff --git a/owned.py b/owned.py\n"
        "--- a/owned.py\n"
        "+++ b/owned.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```\nRESULT: DONE"
    )
    transport = FakeTransport(200, _ok_response(response))
    exe = DirectGatewayExecutor(base_url="http://fake:20128", api_key="k")
    result = await _run_with_transport(
        exe, transport, _implement_prompt(["owned.py"]), "cc/test", tmp_path
    )
    assert result.ok, f"legitimate diff must succeed, got error: {result.error}"
    assert (tmp_path / "owned.py").read_text() == "new\n"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"VERDICT_OMNIROUTE_API_KEY": "v", "OMNIROUTE_API_KEY": "o", "OPENAI_API_KEY": "a"}, "v"),
        ({"OMNIROUTE_API_KEY": "o", "OPENAI_API_KEY": "a"}, "o"),
        ({"OPENAI_API_KEY": "a"}, "a"),
        ({"VERDICT_OMNIROUTE_API_KEY": "  ", "OPENAI_API_KEY": "a"}, "a"),
        ({}, ""),
    ],
)
def test_key_env_precedence_matches_rest_of_verdict(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """Regression: certify sets VERDICT_OMNIROUTE_API_KEY; the executor must send it."""
    for name in ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    executor = DirectGatewayExecutor()
    assert executor.api_key == expected
    headers = executor._headers()
    if expected:
        assert headers["authorization"] == f"Bearer {expected}"
    else:
        assert "authorization" not in headers


def test_explicit_key_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "env")
    assert DirectGatewayExecutor(api_key="explicit").api_key == "explicit"
