"""BOD-219: OmniRoute evidence-safety headers and response-anomaly fail-closed.

OmniRoute can answer a ``temperature: 0`` request from its exact-match cache,
rewrite messages (compression, memory/skills injection) or answer with a
different model (emergency fallback). Verdict sends the per-request opt-outs on
every inference request it owns and never counts a cached or fallback answer as
probe proof. Every transport here is a stub: no test opens a connection.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict.http_safety import (
    OMNIROUTE_EVIDENCE_HEADERS,
    omniroute_evidence_headers,
    omniroute_response_anomalies,
    untrusted_response_reason,
)


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on any real connection. ``httpx.MockTransport`` opens none."""
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"test opened a connection: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
SAFETY = {
    "x-omniroute-no-cache": "true",
    "x-omniroute-compression": "off",
    "x-omniroute-no-memory": "true",
}
CACHE_HIT = {"X-OmniRoute-Cache": "HIT"}
FALLBACK = {"X-OmniRoute-Emergency-Fallback": "true"}


def _lower(headers: Mapping[str, str] | Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _assert_safety(headers: Mapping[str, str] | Any) -> str:
    """Assert the three opt-outs are present and return the request id."""
    lowered = _lower(headers)
    for key, value in SAFETY.items():
        assert lowered.get(key) == value, (key, lowered)
    request_id = lowered.get("x-request-id", "")
    assert request_id
    return request_id


class _UrllibResponse(io.BytesIO):
    """A stand-in for ``http.client.HTTPResponse`` with real ``Message`` headers."""

    def __init__(
        self, body: Mapping[str, Any], headers: Mapping[str, str] | None = None, status: int = 200
    ) -> None:
        super().__init__(json.dumps(body).encode("utf-8"))
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self) -> _UrllibResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _chat_body(content: str = "OK", model: str = "m") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


# --------------------------------------------------------------- shared helper


def test_evidence_headers_carry_every_opt_out_and_a_fresh_request_id() -> None:
    first = omniroute_evidence_headers()
    second = omniroute_evidence_headers()
    assert _assert_safety(first) != _assert_safety(second)
    assert _lower(OMNIROUTE_EVIDENCE_HEADERS) == SAFETY
    # The shared constant is read-only, so no caller can weaken it in place.
    with pytest.raises(TypeError):
        OMNIROUTE_EVIDENCE_HEADERS["X-OmniRoute-No-Cache"] = "false"  # type: ignore[index]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, ()),
        (None, ()),
        ({"X-OmniRoute-Cache": "MISS"}, ()),
        ({"X-OmniRoute-Compression": "off; source=request"}, ()),
        ({"X-OmniRoute-Emergency-Fallback": "false"}, ()),
        ({"X-OmniRoute-Cache": "HIT"}, ("cache_hit",)),
        ({"x-omniroute-cache": "hit"}, ("cache_hit",)),
        ({"x-omniroute-cache-hit": "true"}, ("cache_hit",)),
        ({"X-OmniRoute-Compression": "lite; source=config"}, ("compressed",)),
        ({"X-OmniRoute-Emergency-Fallback": "true"}, ("emergency_fallback",)),
        (
            {
                "X-OmniRoute-Cache": "HIT",
                "X-OmniRoute-Compression": "aggressive; source=combo",
                "X-OmniRoute-Emergency-Fallback": "glm/glm-5",
            },
            ("cache_hit", "compressed", "emergency_fallback"),
        ),
    ],
)
def test_response_anomalies_are_named(
    headers: Mapping[str, str] | None, expected: tuple[str, ...]
) -> None:
    assert omniroute_response_anomalies(headers) == expected


def test_response_anomalies_read_urllib_and_httpx_header_types() -> None:
    message = Message()
    message["X-OmniRoute-Cache"] = "HIT"
    assert omniroute_response_anomalies(message) == ("cache_hit",)
    assert omniroute_response_anomalies(httpx.Headers(FALLBACK)) == ("emergency_fallback",)


def test_a_clean_omniroute_3_8_53_reply_has_no_anomaly() -> None:
    """The exact values OmniRoute 3.8.53 sends on an upstream (non-cached) reply."""
    clean = {
        "X-OmniRoute-Cache": "MISS",
        "X-OmniRoute-Cache-Hit": "false",
        "X-OmniRoute-Compression": "off; source=request-header",
    }
    assert omniroute_response_anomalies(clean) == ()
    assert omniroute_response_anomalies({"X-OmniRoute-Compression": "off"}) == ()


_UNKNOWN_VALUES = ("no", "0", "", "  ", "garbage", "none", "disabled", "off", "true")


@pytest.mark.parametrize("value", (*_UNKNOWN_VALUES, "BYPASS", "miss", "HIT", "hit"))
def test_cache_marker_is_safe_only_when_exactly_miss(value: str) -> None:
    assert omniroute_response_anomalies({"X-OmniRoute-Cache": value}) == ("cache_hit",)


@pytest.mark.parametrize("value", (*_UNKNOWN_VALUES, "TRUE", "False"))
def test_cache_hit_flag_is_safe_only_when_exactly_false(value: str) -> None:
    assert omniroute_response_anomalies({"X-OmniRoute-Cache-Hit": value}) == ("cache_hit",)


@pytest.mark.parametrize(
    "value",
    ("no", "0", "", "garbage", "none", "disabled", "false", "OFF", "offline; source=x", "lite"),
)
def test_compression_marker_is_safe_only_when_its_mode_is_off(value: str) -> None:
    assert omniroute_response_anomalies({"X-OmniRoute-Compression": value}) == ("compressed",)


@pytest.mark.parametrize(
    "value", (*_UNKNOWN_VALUES, "False", "FALSE", "from=glm/glm-5; to=free/model")
)
def test_emergency_fallback_marker_is_safe_only_when_exactly_false(value: str) -> None:
    assert omniroute_response_anomalies({"X-OmniRoute-Emergency-Fallback": value}) == (
        "emergency_fallback",
    )


def test_a_repeated_marker_is_unsafe_when_any_value_is_unsafe() -> None:
    message = Message()
    message["X-OmniRoute-Cache"] = "MISS"
    message["X-OmniRoute-Cache"] = "HIT"
    assert omniroute_response_anomalies(message) == ("cache_hit",)
    repeated = httpx.Headers([("x-omniroute-cache", "MISS"), ("x-omniroute-cache", "HIT")])
    assert omniroute_response_anomalies(repeated) == ("cache_hit",)


def test_only_cache_hit_and_emergency_fallback_void_proof() -> None:
    assert untrusted_response_reason(()) is None
    assert untrusted_response_reason(("compressed",)) is None
    assert untrusted_response_reason(("cache_hit",)) == "omniroute_cache_hit"
    assert untrusted_response_reason(("compressed", "emergency_fallback")) == (
        "omniroute_emergency_fallback"
    )


# -------------------------------------- verdict probe / lazy probes (probes.py)


def _probe_opener(sent: list[Any], headers: Mapping[str, str] | None = None) -> Any:
    def opener(request: Any, timeout: float) -> _UrllibResponse:
        del timeout
        sent.append(request)
        return _UrllibResponse(_chat_body(), headers)

    return opener


def test_openai_probe_transport_sends_safety_headers_per_request() -> None:
    from verdict.probes import ProbePolicy, openai_probe_transport

    sent: list[Any] = []
    transport = openai_probe_transport(
        "http://gw/v1",
        opener=_probe_opener(sent),
        # A caller cannot switch the evidence opt-outs back off.
        extra_headers={"X-OmniRoute-No-Cache": "false", "X-Extra": "kept"},
    )
    transport("p/m", ProbePolicy().payload("p/m"), 1.0)
    transport("p/m", ProbePolicy().payload("p/m"), 1.0)
    ids = {_assert_safety(request.headers) for request in sent}
    assert len(ids) == 2
    assert _lower(sent[0].headers)["x-extra"] == "kept"


def test_openai_probe_transport_reports_response_anomalies() -> None:
    from verdict.probes import ProbePolicy, openai_probe_transport

    transport = openai_probe_transport("http://gw/v1", opener=_probe_opener([], CACHE_HIT))
    response = transport("p/m", ProbePolicy().payload("p/m"), 1.0)
    assert response["omniroute_anomalies"] == ("cache_hit",)
    clean = openai_probe_transport("http://gw/v1", opener=_probe_opener([]))
    assert clean("p/m", ProbePolicy().payload("p/m"), 1.0)["omniroute_anomalies"] == ()


@pytest.mark.parametrize(
    ("anomalies", "reason"),
    [
        (("cache_hit",), "omniroute_cache_hit"),
        (("emergency_fallback",), "omniroute_emergency_fallback"),
    ],
)
def test_probe_runner_never_counts_a_cached_or_fallback_answer_as_ready(
    anomalies: tuple[str, ...], reason: str
) -> None:
    from verdict.availability import AvailabilityState, normalize_observation
    from verdict.models import ModelInfo
    from verdict.probes import ProbeRunner

    def transport(model_id: str, payload: Any, timeout: float) -> dict[str, Any]:
        return {"status_code": 200, "body": _chat_body(), "omniroute_anomalies": anomalies}

    result = ProbeRunner().run(["runtime/model"], transport)[0]
    assert result.availability_state != "ready"
    assert result.status == "failed"
    assert result.error_class == reason
    # Not a model failure: a replayed answer must not walk the route to quarantine.
    assert result.consecutive_failures == 0
    model = ModelInfo(id="runtime/model", provider="runtime", model="model", capability_tier=2)
    state = normalize_observation(model, result.as_runtime_observation(), now=result.observed_at)
    assert state.state is AvailabilityState.UNAVAILABLE


def test_probe_runner_keeps_a_compressed_answer_as_an_observation_only() -> None:
    from verdict.probes import ProbeRunner

    def transport(model_id: str, payload: Any, timeout: float) -> dict[str, Any]:
        return {"status_code": 200, "body": _chat_body(), "omniroute_anomalies": ("compressed",)}

    assert ProbeRunner().run(["runtime/model"], transport)[0].availability_state == "ready"


# --------------------------- prove-at-rest chat/tool + verified-refresh probes


def _patch_prove_open(
    monkeypatch: pytest.MonkeyPatch,
    sent: list[Any],
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> None:
    def fake_open(request: Any, *, timeout: float) -> _UrllibResponse:
        del timeout
        sent.append(request)
        return _UrllibResponse(body or _chat_body(), headers)

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", fake_open)


def test_prove_at_rest_live_transport_sends_safety_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from verdict.prove_at_rest import live_transport

    sent: list[Any] = []
    _patch_prove_open(monkeypatch, sent)
    transport = live_transport("http://gw", api_key="k")
    assert transport("p/m", "chat", 1.0).ok
    transport("p/m", "tool", 1.0)
    ids = {_assert_safety(request.headers) for request in sent}
    assert len(ids) == 2


@pytest.mark.parametrize(
    ("headers", "reason"),
    [(CACHE_HIT, "omniroute_cache_hit"), (FALLBACK, "omniroute_emergency_fallback")],
)
def test_prove_at_rest_cached_or_fallback_chat_is_not_proof(
    monkeypatch: pytest.MonkeyPatch, headers: Mapping[str, str], reason: str
) -> None:
    from verdict.prove_at_rest import (
        NO_HEALTH_WRITE_CATEGORIES,
        AdmittedRoute,
        live_transport,
        probe_full,
        probe_liveness,
    )

    _patch_prove_open(monkeypatch, [], headers)
    transport = live_transport("http://gw", api_key="k")
    exchange = transport("p/m", "chat", 1.0)
    assert not exchange.ok
    assert exchange.error_category == reason
    assert reason in NO_HEALTH_WRITE_CATEGORIES

    route = AdmittedRoute("p/m", "p", "free")
    for prober in (probe_full, probe_liveness):
        outcome = prober(route, transport, timeout_seconds=1.0)
        assert outcome.result is None  # no proof written, prior proof left to age out
        assert outcome.no_write
        assert outcome.no_write_reason == reason


def test_prove_at_rest_cached_tool_call_is_not_tool_proof() -> None:
    from verdict.prove_at_rest import AdmittedRoute, ProbeExchange, probe_full

    def transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        if phase == "chat":
            return ProbeExchange(http_status=200, ok=True, chat_exact=True, reported_model="m")
        return ProbeExchange(http_status=200, ok=False, error_category="omniroute_cache_hit")

    outcome = probe_full(AdmittedRoute("p/m", "p", "free"), transport, timeout_seconds=1.0)
    assert outcome.result is None
    assert outcome.requests_made == 2
    assert outcome.no_write_reason == "omniroute_cache_hit"


def test_verified_refresh_lazy_transport_is_the_safe_live_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.actions import verified_models

    sent: list[Any] = []
    _patch_prove_open(monkeypatch, sent)
    monkeypatch.setattr("verdict.orchestration.run.resolve_api_key", lambda *a, **k: "k")
    transport = verified_models._lazy_live_transport("http://gw")
    transport("p/m", "chat", 1.0)
    assert len(sent) == 1
    _assert_safety(sent[0].headers)


# ------------------------------------------------------- prove-at-rest agentic


def test_prove_at_rest_agentic_transport_sends_headers_and_flags_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.prove_at_rest import live_agentic_transport

    sent: list[Any] = []
    _patch_prove_open(monkeypatch, sent, CACHE_HIT)
    exchange = live_agentic_transport("http://gw", api_key="k")("p/m", {"model": "p/m"}, 1.0)
    _assert_safety(sent[0].headers)
    assert not exchange.ok
    assert exchange.error_category == "omniroute_cache_hit"


def _no_chat(route_id: str, phase: str, timeout: float) -> Any:
    raise AssertionError("the agentic phase must not use the chat/tool transport")


def _agentic_gateway(
    monkeypatch: pytest.MonkeyPatch, sent: list[Any], *, marked_turn: int | None
) -> None:
    """A stub gateway that does the read/edit/read task through the REAL agentic
    transport; turn ``marked_turn`` (1-based) carries the emergency-fallback marker."""
    from tests.test_prove_at_rest_agentic_budget import _turn

    def fake_open(request: Any, *, timeout: float) -> _UrllibResponse:
        del timeout
        payload = json.loads(request.data)
        sent.append(payload)
        body = _turn(payload["model"], payload).response_body
        assert body is not None
        return _UrllibResponse(body, FALLBACK if len(sent) == marked_turn else None)

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", fake_open)


@pytest.mark.parametrize("marked_turn", [None, 3])
def test_prove_at_rest_agentic_fallback_turn_writes_no_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked_turn: int | None
) -> None:
    """Turns 1-2 are good; turn 3 is answered by the emergency fallback.

    ``marked_turn=None`` is the control: the same path does write a passing
    agentic capability, so the fallback case is not passing by accident.
    """
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import AdmittedRoute, CycleStats, Prober, live_agentic_transport

    sent: list[Any] = []
    _agentic_gateway(monkeypatch, sent, marked_turn=marked_turn)
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    prober = Prober(
        cache,
        lambda: [AdmittedRoute("p/m", "p", "free")],
        _no_chat,
        agentic_transport=live_agentic_transport("http://gw", api_key="k"),
        clock=lambda: NOW,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert len(sent) == 3  # every turn, the marked one included, really ran
    on_disk = HealthCache(path).entry("p/m")
    if marked_turn is None:
        assert on_disk is not None and on_disk.agentic_ok
        assert stats.agentic_passed == 1
        return
    assert cache.entry("p/m") is None
    assert on_disk is None
    assert stats.agentic_probed == 0
    assert stats.agentic_passed == 0


def test_identify_screen_decides_before_it_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``screen_route`` must not enter the cache-write path for a fallback turn."""
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import AdmittedRoute, Prober, live_agentic_transport

    sent: list[Any] = []
    _agentic_gateway(monkeypatch, sent, marked_turn=3)
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    writes: list[str] = []
    real_merge = cache.merge_and_save

    def spy(mutate: Any, **kwargs: Any) -> None:
        writes.append(mutate.__qualname__)
        real_merge(mutate, **kwargs)

    monkeypatch.setattr(cache, "merge_and_save", spy)
    prober = Prober(
        cache,
        lambda: [],
        _no_chat,
        agentic_transport=live_agentic_transport("http://gw", api_key="k"),
        clock=lambda: NOW,
    )
    outcome = prober.screen_route(AdmittedRoute("p/m", "p", "free"), deadline=1e12)
    assert outcome["turns_sent"] == 3
    assert outcome["result"] == "omniroute_emergency_fallback"
    assert any("reserve_bucket" in name for name in writes)  # the spy saw the budget write
    assert not any("screen_route" in name for name in writes), writes
    assert HealthCache(path).entry("p/m") is None


def _probe_gateway(
    monkeypatch: pytest.MonkeyPatch, sent: list[Any], marker: Mapping[str, str] | None
) -> None:
    """A stub gateway answering the chat probe with OK and the tool probe with the call."""
    from verdict.prove_at_rest import TOOL_NAME

    def fake_open(request: Any, *, timeout: float) -> _UrllibResponse:
        del timeout
        payload = json.loads(request.data)
        sent.append(payload)
        if payload.get("tools"):
            call = {
                "id": "c1",
                "type": "function",
                "function": {"name": TOOL_NAME, "arguments": "{}"},
            }
            body = {"model": "m", "choices": [{"message": {"content": None, "tool_calls": [call]}}]}
        else:
            body = _chat_body()
        return _UrllibResponse(body, marker)

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", fake_open)


@pytest.mark.parametrize("marker", [None, CACHE_HIT, FALLBACK])
def test_prove_at_rest_cycle_never_refreshes_proof_from_a_cached_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: Mapping[str, str] | None
) -> None:
    """Real daemon cycle (``Prober.run_once`` -> RefreshCoordinator -> probe_full).

    The route holds earlier, still-usable proof that is due for refresh (a
    FRESH entry is never re-probed). A clean reply (control) refreshes it; a
    cached or fallback reply leaves the stored entry exactly as it was.
    """
    from verdict.orchestration.health_cache import (
        EVIDENCE_FRESH_SECONDS,
        STATE_STALE,
        HealthCache,
        ProbeResult,
    )
    from verdict.prove_at_rest import AdmittedRoute, Prober, live_transport

    path = tmp_path / "health.json"
    seeded = HealthCache(path)
    proved_at = NOW - timedelta(seconds=EVIDENCE_FRESH_SECONDS + 60)
    seeded.record("p/m", ProbeResult("ok", True, True, identity="verified"), proved_at)
    seeded.save()
    before = HealthCache(path).entry("p/m")
    assert before is not None and before.state_at(NOW) == STATE_STALE

    sent: list[Any] = []
    _probe_gateway(monkeypatch, sent, marker)
    Prober(
        HealthCache(path),
        lambda: [AdmittedRoute("p/m", "p", "free")],
        live_transport("http://gw", api_key="k"),
        clock=lambda: NOW,
    ).run_once()
    after = HealthCache(path).entry("p/m")
    assert sent, "the cycle must really probe the route"
    assert after is not None
    if marker is None:
        assert after.checked_at == NOW  # control: a clean reply does refresh
        return
    assert len(sent) == 1  # the chat phase stopped; no tool request
    assert after.to_dict() == before.to_dict()


@pytest.mark.parametrize("marker", [CACHE_HIT, FALLBACK])
def test_prove_at_rest_cycle_writes_nothing_for_an_unproven_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: Mapping[str, str]
) -> None:
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import AdmittedRoute, Prober, live_transport

    path = tmp_path / "health.json"
    sent: list[Any] = []
    _probe_gateway(monkeypatch, sent, marker)
    Prober(
        HealthCache(path),
        lambda: [AdmittedRoute("p/m", "p", "free")],
        live_transport("http://gw", api_key="k"),
        clock=lambda: NOW,
    ).run_once()
    assert len(sent) == 1
    assert HealthCache(path).entry("p/m") is None


# ------------------------------------------------------------ acceptance check


def test_acceptance_toolcheck_sends_headers_and_fails_cached_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict import acceptance_toolcheck

    sent: list[Any] = []

    def fake_open(request: Any, *, timeout: float) -> _UrllibResponse:
        del timeout
        sent.append(request)
        return _UrllibResponse(_chat_body(), CACHE_HIT)

    monkeypatch.setattr(acceptance_toolcheck, "open_no_redirect", fake_open)
    post = acceptance_toolcheck.gateway_post("http://gw", "k")
    check = acceptance_toolcheck.check_route("p/m", post)
    _assert_safety(sent[0].headers)
    assert check.outcome == "fail"
    assert check.detail == "turn1: omniroute_cache_hit"


# -------------------------------------------------- eligibility health probes


def test_eligibility_health_probe_sends_headers_and_fails_closed() -> None:
    from verdict.subagent_selection import LaunchCandidate, openai_health_probe

    sent: list[Any] = []
    candidate = LaunchCandidate("p/m", "omniroute/p/m", frozenset(), 1000, 0, 0, True, False, 0, 0)
    healthy = openai_health_probe("http://gw/v1", opener=_probe_opener(sent))(candidate)
    assert healthy.healthy
    fallback = openai_health_probe("http://gw/v1", opener=_probe_opener(sent, FALLBACK))(candidate)
    assert not fallback.healthy
    assert fallback.category == "omniroute_emergency_fallback"
    assert len({_assert_safety(request.headers) for request in sent}) == 2


# ------------------------------------------------------------ census needle


def test_census_needle_probe_sends_headers_and_fails_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.omniroute_pipeline.census_hook import needle_prober
    from verdict.omniroute_pipeline.probes import ProbeOutcome

    posted: list[dict[str, str]] = []

    def _post(endpoint: str, **kw: Any) -> httpx.Response:
        posted.append(kw["headers"])
        needle_text = json.dumps(kw["json"])
        code = needle_text.split("The secret code is ", 1)[1].split(".", 1)[0]
        return httpx.Response(
            200,
            json=_chat_body(f"The code is {code}"),
            headers=FALLBACK,
            request=httpx.Request("POST", endpoint),
        )

    monkeypatch.setattr(httpx, "post", _post)
    prober = needle_prober("http://gw/v1", "k")
    assert prober("p/m", 128_000) is ProbeOutcome.FAIL
    prober("p/m", 128_000)
    assert len({_assert_safety(headers) for headers in posted}) == 2


# ------------------------------------------------------ DirectGatewayExecutor


def test_direct_gateway_executor_sends_safety_headers_per_request() -> None:
    from verdict.orchestration.executors import DirectGatewayExecutor

    executor = DirectGatewayExecutor(base_url="http://gw:20128", api_key="k")
    first = executor._headers()
    second = executor._headers()
    assert _assert_safety(first) != _assert_safety(second)
    assert first["authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_direct_gateway_executor_requests_carry_safety_headers(tmp_path: Path) -> None:
    from verdict.orchestration.executors import DirectGatewayExecutor

    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json={**_chat_body("Hello"), "model": "p/m"})

    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    executor = DirectGatewayExecutor(base_url="http://gw:20128", api_key="k")
    prompt = "GOAL: t\n\nNODE: n\nOBJECTIVE: o\nOWNED_FILES: (none)\n\nsay hello"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "AsyncClient", client)
        terminal = await executor.run(prompt, route_id="p/m", cwd=tmp_path, timeout_seconds=5.0)
    assert terminal.ok, terminal.error
    assert len(seen) == 1
    _assert_safety(seen[0])


# --------------------------------------------------------- relay upstream proxy


def test_relay_upstream_proxy_sends_safety_headers_on_inference_only() -> None:
    from verdict.proxy import UpstreamProxy

    seen: list[tuple[str, httpx.Headers]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers))
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=_chat_body())

    proxy = UpstreamProxy(
        "https://upstream.example/v1",
        transport=httpx.MockTransport(handler),
        allow_private_hosts={"upstream.example"},
    )
    asyncio.run(proxy.chat({"model": "p/m", "messages": []}))
    asyncio.run(proxy.chat({"model": "p/m", "messages": []}))
    asyncio.run(proxy.responses({"model": "p/m", "input": "hi"}))
    asyncio.run(proxy.models())
    inference = [headers for path, headers in seen if not path.endswith("/models")]
    assert len({_assert_safety(headers) for headers in inference}) == 3
    models_headers = next(headers for path, headers in seen if path.endswith("/models"))
    assert "x-omniroute-no-cache" not in models_headers
