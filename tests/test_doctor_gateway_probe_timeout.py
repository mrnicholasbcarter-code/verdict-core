"""verdict doctor: gateway probes fit a slow but healthy gateway.

OmniRoute can answer its first authenticated ``/models`` call in 4 s under load
while ``/api/health`` answers in milliseconds. Doctor must not call that gateway
unreachable, must retry a timeout exactly once, must never retry a real answer
(401, 403, 5xx), and must name the timeout and its setting when it gives up.
A local stub server stands in for the gateway; no live gateway is contacted.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

import verdict.doctor_diagnostics as dd
import verdict.provider_bootstrap as pb

# Literal names, so the suite pins the public setting names.
GATEWAY_ENV = "VERDICT_DOCTOR_GATEWAY_TIMEOUT_S"
HEALTH_ENV = "VERDICT_DOCTOR_HEALTH_TIMEOUT_S"


@dataclass
class Stub:
    delay_s: float = 0.0
    status: int = 200
    hits: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    url: str = ""


@pytest.fixture
def stub() -> Iterator[Stub]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.hits.append(self.path)
            if state.stop.wait(state.delay_s):
                return
            body = b'{"data": []}'
            try:
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the client gave up first

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        state.stop.set()
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GATEWAY_ENV, raising=False)
    monkeypatch.delenv(HEALTH_ENV, raising=False)
    monkeypatch.setenv("OMNIROUTE_API_KEY", "stub-key")
    monkeypatch.setattr(pb, "load_credential_store_env", lambda: {})


def _doctor_probe(url: str) -> Any:
    binding = SimpleNamespace(base_url=f"{url}/v1", api_key_env="OMNIROUTE_API_KEY")
    bootstrap = SimpleNamespace(providers={"omniroute": binding}, gateway_url=url)
    return dd._gateway_probe_for(bootstrap)(url)


def test_models_answering_after_3s_is_healthy(stub: Stub) -> None:
    """Fails when the probe timeout is shorter than a loaded gateway's first answer."""
    stub.delay_s = 3.0
    health = _doctor_probe(stub.url)
    assert health.reachable is True, health.detail
    assert stub.hits == ["/models"]


def test_models_never_answering_fails_naming_timeout_and_setting(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails when a hung gateway is reported healthy or the message hides the knob."""
    monkeypatch.setenv(GATEWAY_ENV, "0.3")
    stub.delay_s = 30.0
    health = _doctor_probe(stub.url)
    assert health.reachable is False
    assert "0.3s" in health.detail
    assert GATEWAY_ENV in health.detail


def test_models_timeout_is_retried_exactly_once(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails when a timeout gets no retry, or more than one."""
    monkeypatch.setenv(GATEWAY_ENV, "0.3")
    stub.delay_s = 30.0
    _doctor_probe(stub.url)
    assert stub.hits == ["/models", "/models"]


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_models_answer_is_never_retried_or_healthy(stub: Stub, status: int) -> None:
    """Fails when a real non-2xx answer is retried or counted as healthy."""
    stub.status = status
    health = _doctor_probe(stub.url)
    assert health.reachable is False
    assert stub.hits == ["/models"]


def test_models_timeout_setting_changes_the_timeout(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails when the environment override is ignored."""
    stub.delay_s = 1.0
    monkeypatch.setenv(GATEWAY_ENV, "0.3")
    assert _doctor_probe(stub.url).reachable is False


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "nan", "inf", "1000"])
def test_invalid_timeout_setting_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Fails when a typo can hang doctor or make every probe fail instantly."""
    monkeypatch.setenv(GATEWAY_ENV, raw)
    assert (
        dd.doctor_probe_timeout(GATEWAY_ENV, dd.DEFAULT_DOCTOR_GATEWAY_TIMEOUT_S)
        == dd.DEFAULT_DOCTOR_GATEWAY_TIMEOUT_S
    )


def test_health_slow_answer_within_timeout_passes(stub: Stub) -> None:
    """Fails when the /api/health check keeps a 2 s ceiling."""
    stub.delay_s = 2.5
    assert dd.gateway_health_failure(stub.url, timeout=dd.DEFAULT_DOCTOR_HEALTH_TIMEOUT_S) is None


def test_health_timeout_retried_once_then_names_setting(stub: Stub) -> None:
    """Fails when the health timeout is not retried once or the message hides the knob."""
    stub.delay_s = 30.0
    failure = dd.gateway_health_failure(stub.url, timeout=0.3)
    assert failure is not None
    assert "0.3s" in failure and HEALTH_ENV in failure
    assert stub.hits == ["/api/health", "/api/health"]


@pytest.mark.parametrize("status", [401, 500])
def test_health_error_status_is_not_retried_or_healthy(stub: Stub, status: int) -> None:
    """Fails when an error status passes or is retried."""
    stub.status = status
    failure = dd.gateway_health_failure(stub.url, timeout=1.0)
    assert failure is not None
    assert stub.hits == ["/api/health"]
