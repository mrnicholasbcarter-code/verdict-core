"""An HTTP 200 whose body is a credit refusal is a payment failure, not a success.

Shape taken from a live capture (2026-11-18): the provider answered HTTP 200,
``finish_reason`` stop, zero completion tokens, no tool calls, and a text that
says the account has no credits. Every test here uses that redacted shape with
stub transports; nothing calls a provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests import test_serve_failure_feedback as feedback
from verdict import acceptance
from verdict import omniroute_webhook as wh
from verdict.orchestration.health_cache import (
    CATEGORY_PAYMENT,
    STATE_NEGATIVE,
    HealthCache,
    ProbeResult,
    default_cache_path,
)
from verdict.orchestration.provider_catalog import is_inband_billing_refusal, is_not_free_signal
from verdict.prove_at_rest import (
    AdmittedRoute,
    _exchange_from_body,
    category_for,
    live_agentic_transport,
    probe_full,
    probe_liveness,
)

REFUSAL_TEXT = (
    "The account behind this API key doesn't have enough credits. "
    "Please [top up](<https://provider.example/top-up>) or complete a quest, then try again."
)


def refusal_body(model: str = "openai/gpt-5.5") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": REFUSAL_TEXT, "tool_calls": None},
            }
        ],
        "usage": {"prompt_tokens": 174, "completion_tokens": 0, "total_tokens": 174},
    }


def answer_body(model: str, content: str = "OK", *, completion_tokens: int = 1) -> dict[str, Any]:
    body = refusal_body(model)
    body["choices"][0]["message"]["content"] = content
    body["usage"]["completion_tokens"] = completion_tokens
    return body


# -- shared detector ----------------------------------------------------------


def test_detector_flags_the_captured_refusal_as_mapping_and_bytes() -> None:
    assert is_inband_billing_refusal(refusal_body())
    assert is_inband_billing_refusal(json.dumps(refusal_body()).encode())


def test_detector_ignores_a_real_answer_that_quotes_the_phrase() -> None:
    # A model that wrote the words itself spent completion tokens.
    assert not is_inband_billing_refusal(answer_body("m", REFUSAL_TEXT, completion_tokens=31))


def test_detector_needs_zero_output_evidence_and_no_tool_call() -> None:
    no_usage = refusal_body()
    del no_usage["usage"]
    assert not is_inband_billing_refusal(no_usage)
    with_tool = refusal_body()
    with_tool["choices"][0]["message"]["tool_calls"] = [
        {"type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    ]
    assert not is_inband_billing_refusal(with_tool)
    assert not is_inband_billing_refusal(answer_body("m", "OK", completion_tokens=0))
    assert not is_inband_billing_refusal(b"not json")
    assert not is_inband_billing_refusal(None)


def test_not_free_phrases_cover_the_refusal_wording() -> None:
    assert is_not_free_signal(error_message=REFUSAL_TEXT)
    assert not is_not_free_signal(error_message="upstream timed out")


# -- probe exchanges ----------------------------------------------------------


def test_chat_probe_exchange_is_a_payment_failure() -> None:
    exchange = _exchange_from_body(
        200, refusal_body(), phase="chat", latency_ms=12.0, retry_after=None
    )
    assert not exchange.ok and not exchange.chat_exact
    assert exchange.error_category == "payment_required"
    assert category_for(exchange) == CATEGORY_PAYMENT


class _Response:
    status = 200

    def __init__(self, body: dict[str, Any]) -> None:
        self._raw = json.dumps(body).encode()
        self.headers = Message()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._raw


def test_agentic_transport_marks_the_refusal_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "verdict.prove_at_rest.open_no_redirect",
        lambda request, *, timeout: _Response(refusal_body()),
    )
    exchange = live_agentic_transport("http://gw", api_key="k")("pol/m", {"model": "pol/m"}, 1.0)
    assert exchange.http_status == 200
    assert not exchange.ok
    assert exchange.error_category == "payment_required"


@pytest.mark.parametrize("prober", [probe_full, probe_liveness])
def test_probe_records_a_route_scoped_payment_negative(prober: Any) -> None:
    def transport(route_id: str, phase: str, timeout: float) -> Any:
        return _exchange_from_body(
            200, refusal_body(), phase=phase, latency_ms=5.0, retry_after=None
        )

    outcome = prober(
        AdmittedRoute("pol/openai/gpt-5.5", "pol", "free"), transport, timeout_seconds=1.0
    )
    assert outcome.requests_made == 1  # no tool turn after a refused chat
    assert outcome.result is not None and not outcome.result.chat_ok
    assert outcome.category == CATEGORY_PAYMENT
    assert outcome.result.failure_scope == "route"
    assert outcome.result.capacity_evidence == "billing_402_metered"
    # Free siblings on the same key still answer: never a provider-wide stop.
    assert outcome.model_scoped


def test_agentic_probe_writes_a_route_scoped_metered_negative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The at-rest agentic probe is what kept the dead routes qualified."""
    from verdict.prove_at_rest import CycleStats, Prober, _cooled_pools

    sent: list[Any] = []

    def fake_open(request: Any, *, timeout: float) -> _Response:
        sent.append(request)
        return _Response(refusal_body())

    monkeypatch.setattr("verdict.prove_at_rest.open_no_redirect", fake_open)

    def no_chat(route_id: str, phase: str, timeout: float) -> Any:
        raise AssertionError("the agentic phase must not use the chat/tool transport")

    cache = HealthCache(tmp_path / "health.json")
    prober = Prober(
        cache,
        lambda: [AdmittedRoute("pol/openai/gpt-5.5", "pol", "free")],
        no_chat,
        agentic_transport=live_agentic_transport("http://gw", api_key="k"),
        clock=lambda: NOW,
    )
    stats = CycleStats()
    prober.run_agentic_probes(stats)
    assert len(sent) == 1  # the conversation stops at the refused turn
    assert stats.agentic_passed == 0
    entry = HealthCache(cache.path).entry("pol/openai/gpt-5.5")
    assert entry is not None and not entry.healthy and not entry.agentic_ok
    assert entry.category == CATEGORY_PAYMENT and not entry.chat_ok
    assert entry.failure_scope == "route"
    assert entry.capacity_evidence == "billing_402_metered"
    assert entry.state_at(NOW + timedelta(hours=1)) == STATE_NEGATIVE
    assert _cooled_pools(cache, NOW) == set()  # free siblings on the key stay open


# -- relay --------------------------------------------------------------------


def test_relay_fails_over_from_a_refusal_and_marks_the_route_metered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        if len(sent) == 1:
            return httpx.Response(200, json=refusal_body(model))
        return httpx.Response(200, json=answer_body(model))

    prepared = feedback._prepare(monkeypatch, transport)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=feedback._request_body(),
            headers={"idempotency-key": "inband-failover"},
        )
    assert len(sent) == 2 and sent[0] != sent[1], "the refusal must count as a failed attempt"
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "OK"
    assert response.headers["x-verdict-model"] == sent[1]
    cache = HealthCache(default_cache_path())
    entry = cache.entry(feedback._attempted_route(prepared))
    assert entry is not None and not entry.healthy
    assert entry.category == CATEGORY_PAYMENT
    assert entry.failure_scope == "route"
    assert entry.capacity_evidence == "billing_402_metered"
    assert entry.feedback_source == "serve_request"
    assert entry.until - entry.checked_at >= timedelta(hours=6)
    # Route-scoped only: no pool or provider cooldown.
    assert cache.cooldowns() == {}


def test_relay_reports_402_when_the_only_candidate_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        sent.append(model)
        return httpx.Response(200, json=refusal_body(model))

    prepared = feedback._prepare(monkeypatch, transport, alternatives=False)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=feedback._request_body(),
            headers={"idempotency-key": "inband-only"},
        )
    assert len(sent) == 1  # bounded: no candidate left, no repeat of the same route
    assert response.status_code == 402
    assert response.headers["x-verdict-transport-outcome"] == "upstream_error"
    entry = HealthCache(default_cache_path()).entry(feedback._attempted_route(prepared))
    assert entry is not None and entry.capacity_evidence == "billing_402_metered"
    assert HealthCache(default_cache_path()).entry(feedback._other_route(prepared)) is None


# -- passive refresh ----------------------------------------------------------

NOW = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)


def test_traffic_200_does_not_revive_a_route_after_a_billing_negative(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    refused_at = NOW - timedelta(hours=7)
    cache.record(
        "pol/m",
        ProbeResult(category=CATEGORY_PAYMENT, chat_ok=False, tool_ok=False, http_status=200),
        refused_at,
    )
    assert cache.lookup("pol/m", NOW).state != STATE_NEGATIVE  # the 6 h backoff has ended
    outcome = cache.record_passive_traffic(
        "pol/m", observed_at=NOW - timedelta(minutes=5), now=NOW, source="omniroute_traffic"
    )
    assert outcome == "skipped_billing_negative"
    entry = cache.entry("pol/m")
    assert entry is not None and not entry.healthy and not entry.chat_ok


def test_zero_output_webhook_completion_is_not_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import test_omniroute_webhook as hooks
    from tests import test_passive_health as passive

    home = tmp_path / ".verdict"
    monkeypatch.setenv("VERDICT_HOME", str(home))
    hooks._configure(home)
    at = passive.SEEN + timedelta(minutes=20)
    raw, headers = hooks._delivery(
        data={"provider": "alpha", "model": "best", "latencyMs": 9, "tokensOutput": 0}, at=at
    )
    assert wh.accept_delivery(raw, headers, now=passive.NOW)[1]["accepted"] is True
    [event] = wh.read_events()
    assert event.output_tokens == 0

    cache = HealthCache(tmp_path / "health.json")
    report = passive.refresh_health_from_traffic(
        passive.evidence(), cache, now=passive.NOW, webhook_events=[event]
    )
    assert report["webhook_events_used"] == 0
    assert report["skipped"]["skipped_zero_output"] == 1


# -- acceptance serve check ---------------------------------------------------


def test_serve_check_fails_on_a_refusal_body() -> None:
    passed, reason = acceptance._serve_verdict(200, json.dumps(refusal_body("pol/m")))
    assert passed is False
    assert "credit refusal" in reason and "pol/m" in reason


def test_serve_check_still_passes_a_real_reply() -> None:
    passed, reason = acceptance._serve_verdict(200, json.dumps(answer_body("nvidia/m")))
    assert passed is True and reason == "HTTP 200 from nvidia/m"
    reasoning_only = answer_body("nvidia/m")
    reasoning_only["choices"][0]["message"].update(content=None, reasoning_content="thinking OK")
    assert acceptance._serve_verdict(200, json.dumps(reasoning_only))[0] is True
    assert acceptance._serve_verdict(502, "{}")[0] is False
