"""Local gateway probe contract: auth signals are not outages."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from rich.console import Console

from verdict.home import HomeState, _startup_with_motion, probe_gateway, render_home


@contextmanager
def gateway(status: int, seen: list[str | None], *, delay: float = 0) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            if delay:
                threading.Event().wait(delay)
            body = json.dumps({"data": [{"id": "one"}, {"id": "two"}]}).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except BrokenPipeError:
                pass  # Timeout closed the client side.

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def no_gateway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)


def rendered(url: str, result: tuple[bool | None, int | None, str | None]) -> str:
    state = HomeState(gateway=url)
    state.gateway_ok, state.gateway_models, state.gateway_auth = result
    output = io.StringIO()
    console = Console(file=output, width=110, force_terminal=False)
    console.print(render_home(state, plain=False, width=110))
    console.print(render_home(state, plain=True, width=110))
    _startup_with_motion(console, state, probe_fn=lambda: result, max_sweep_s=0)
    return output.getvalue()


def test_probe_200_without_key() -> None:
    seen: list[str | None] = []
    with gateway(200, seen) as url:
        result = probe_gateway(url)
        assert result == (True, 2, None)
        assert "REACHABLE" in rendered(url, result)
    assert seen == [None]


def test_probe_401_without_key_is_reachable() -> None:
    seen: list[str | None] = []
    with gateway(401, seen) as url:
        result = probe_gateway(url)
        assert result == (True, None, "required")
        output = rendered(url, result)
        assert "REACHABLE (key required)" in output
        assert "UNREACHABLE" not in output
    assert seen == [None]


def test_probe_401_with_bad_key_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "secret-bad-key")
    seen: list[str | None] = []
    with gateway(401, seen) as url:
        result = probe_gateway(url)
        assert result == (True, None, "rejected")
        output = rendered(url, result)
        assert "REACHABLE (key rejected)" in output
        assert "secret-bad-key" not in output
        assert "UNREACHABLE" not in output
    assert seen == ["Bearer secret-bad-key"]


def test_probe_200_with_fallback_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "secret-fallback-key")
    seen: list[str | None] = []
    with gateway(200, seen) as url:
        result = probe_gateway(url)
        assert result == (True, 2, None)
        output = rendered(url, result)
        assert "REACHABLE" in output and "2 models" in output
        assert "secret-fallback-key" not in output
    assert seen == ["Bearer secret-fallback-key"]


def test_probe_timeout_is_unreachable() -> None:
    seen: list[str | None] = []
    with gateway(200, seen, delay=0.2) as url:
        assert probe_gateway(url, timeout=0.01) == (False, None, None)


def test_probe_non_http_scheme_is_not_checked() -> None:
    assert probe_gateway("file:///etc/passwd") == (None, None, None)


def test_probe_403_with_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "secret-denied-key")
    seen: list[str | None] = []
    with gateway(403, seen) as url:
        result = probe_gateway(url)
        assert result == (True, None, "rejected")
        assert "secret-denied-key" not in rendered(url, result)
    assert seen == ["Bearer secret-denied-key"]


def test_probe_prefers_verdict_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "secret-primary")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "secret-secondary")
    seen: list[str | None] = []
    with gateway(200, seen) as url:
        result = probe_gateway(url)
        assert result == (True, 2, None)
        output = rendered(url, result)
        assert "secret-primary" not in output and "secret-secondary" not in output
    assert seen == ["Bearer secret-primary"]
