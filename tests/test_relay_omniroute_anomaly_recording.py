"""Relay replies record OmniRoute response anomalies on their receipts.

The relay passes the reply through unchanged; it only records what
OmniRoute's marker headers say happened (cache hit, compression, emergency
fallback) on the outcome receipt and on the request's evidence outcome event.
Only the fixed anomaly names are stored, never raw header values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests.test_proxy import ChunkedSSE, FixedIntelligence, RecordingTransport, _configure_test_app
from verdict.outcome_log import build_outcome_record, observe_execution, outcome_log_path

LEAK_SENTINEL = "sk-leak-sentinel-0123456789"

MARKED = [
    ("content-type", "application/json"),
    ("X-OmniRoute-Cache", "HIT"),
    ("X-OmniRoute-Emergency-Fallback", f"true; key={LEAK_SENTINEL}"),
]
CLEAN = [
    ("content-type", "application/json"),
    ("X-OmniRoute-Cache", "MISS"),
    ("X-OmniRoute-Cache-Hit", "false"),
    ("X-OmniRoute-Compression", "off; source=request-header"),
    ("X-OmniRoute-Emergency-Fallback", "false"),
]


def test_observe_execution_names_anomalies_the_legacy_cache_field_misses() -> None:
    observed = observe_execution(headers=MARKED, body=None)
    assert observed["omniroute_anomalies"] == ["cache_hit", "emergency_fallback"]
    # The documented ``cache_hit`` field reads only ``X-OmniRoute-Cache-Hit``.
    assert observed["cache_hit"] is None


def test_observe_execution_clean_and_absent_markers_record_no_anomaly() -> None:
    assert observe_execution(headers=CLEAN, body=None)["omniroute_anomalies"] == []
    assert observe_execution(headers={}, body=None)["omniroute_anomalies"] == []


def test_observe_execution_keeps_repeated_markers_fail_closed() -> None:
    repeated = [("x-omniroute-cache", "MISS"), ("x-omniroute-cache", "HIT")]
    assert observe_execution(headers=repeated, body=None)["omniroute_anomalies"] == ["cache_hit"]


def test_outcome_record_carries_anomaly_names_but_no_header_values() -> None:
    record = build_outcome_record(
        request_id="req-1",
        model="selected-model",
        status_code=200,
        attempt=1,
        surface="chat",
        headers=MARKED,
        body=None,
    )
    assert record["omniroute_anomalies"] == ["cache_hit", "emergency_fallback"]
    assert LEAK_SENTINEL not in json.dumps(record)


class MarkedTransport(RecordingTransport):
    def __init__(
        self, marker_headers: list[tuple[str, str]], *, stream: bool = False, status: int = 200
    ) -> None:
        super().__init__(stream=stream)
        self.marker_headers = marker_headers
        self.status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if request.url.path.endswith("/models"):
            return response
        headers = [
            (key, value)
            for key, value in response.headers.multi_items()
            if key.lower() != "content-type"
        ]
        headers += self.marker_headers
        if self.stream:
            chunks = [
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            headers = [(k, v) for k, v in headers if k.lower() != "content-type"]
            headers.append(("content-type", "text/event-stream"))
            return httpx.Response(200, headers=headers, stream=ChunkedSSE(chunks))
        return httpx.Response(self.status, headers=headers, content=await response.aread())


def _logging_app(monkeypatch: pytest.MonkeyPatch, transport: RecordingTransport, log: Path) -> None:
    _configure_test_app(monkeypatch, transport)

    class LoggingIntelligence(FixedIntelligence):
        log_path = str(log)

    monkeypatch.setattr(api, "_build_intelligence", lambda: LoggingIntelligence())


def _relay(stream: bool, correlation_id: str) -> tuple[Any, dict[str, Any]]:
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "stream": stream,
                "correlation_id": correlation_id,
            },
        )
        _ = response.content
        evidence = client.get(f"/v1/route/explain?correlation_id={correlation_id}").json()
    return response, evidence


UPSTREAM_BODY = (
    b'{"id":"chatcmpl-test","model":"selected-model","choices":[],"usage":{"total_tokens":4}}'
)


def _inference_requests(transport: RecordingTransport) -> int:
    return sum(1 for item in transport.requests if not item["url"].endswith("/models"))


def _receipts(log: Path) -> list[dict[str, Any]]:
    path = outcome_log_path(log)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_buffered_relay_records_anomalies_and_still_returns_the_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "verdict-decisions.jsonl"
    transport = MarkedTransport(MARKED)
    _logging_app(monkeypatch, transport, log)

    response, evidence = _relay(False, "relay-marked-buffered")

    # Recorded, not rejected: the client receives the upstream reply byte for
    # byte, with the upstream markers, after one upstream attempt.
    assert response.status_code == 200
    assert response.content == UPSTREAM_BODY
    assert response.headers.get_list("x-omniroute-cache") == ["HIT"]
    # The client sees the upstream fallback marker as sent; only Verdict's own
    # records are restricted to anomaly names.
    assert response.headers.get_list("x-omniroute-emergency-fallback") == [
        f"true; key={LEAK_SENTINEL}"
    ]
    assert _inference_requests(transport) == 1
    details = evidence["outcome_event"]["details"]
    assert details["omniroute_anomalies"] == ["cache_hit", "emergency_fallback"]
    receipts = _receipts(log)
    assert [row["omniroute_anomalies"] for row in receipts] == [["cache_hit", "emergency_fallback"]]
    assert LEAK_SENTINEL not in json.dumps(evidence)
    assert LEAK_SENTINEL not in outcome_log_path(log).read_text()


def test_buffered_relay_clean_reply_records_an_empty_anomaly_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "verdict-decisions.jsonl"
    _logging_app(monkeypatch, MarkedTransport(CLEAN), log)

    response, evidence = _relay(False, "relay-clean-buffered")

    assert response.status_code == 200
    assert evidence["outcome_event"]["details"]["omniroute_anomalies"] == []
    assert [row["omniroute_anomalies"] for row in _receipts(log)] == [[]]


def test_streamed_relay_records_anomalies_on_the_finalized_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "verdict-decisions.jsonl"
    marked = [("X-OmniRoute-Cache", "HIT")]
    _logging_app(monkeypatch, MarkedTransport(marked, stream=True), log)

    response, evidence = _relay(True, "relay-marked-stream")

    assert response.status_code == 200
    assert evidence["outcome_event"]["event_type"] == "chat_completion_streamed"
    assert evidence["outcome_event"]["details"]["omniroute_anomalies"] == ["cache_hit"]
    assert [row["omniroute_anomalies"] for row in _receipts(log)] == [["cache_hit"]]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([("X-OmniRoute-Compression", "lite; source=api-key")], ["compressed"]),
        ([("X-OmniRoute-Cache-Hit", "true")], ["cache_hit"]),
        ([("X-OmniRoute-Cache", "MISS"), ("X-OmniRoute-Cache", "MISS")], []),
        (
            [
                ("X-OmniRoute-Emergency-Fallback", "false"),
                ("x-omniroute-emergency-fallback", "false"),
            ],
            [],
        ),
    ],
    ids=["compressed", "cache-hit-header", "repeated-safe-cache", "repeated-safe-fallback"],
)
def test_observe_execution_covers_every_marker(
    headers: list[tuple[str, str]], expected: list[str]
) -> None:
    assert observe_execution(headers=headers, body=None)["omniroute_anomalies"] == expected


def test_buffered_relay_keeps_a_non_200_status_while_recording_anomalies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "verdict-decisions.jsonl"
    _logging_app(monkeypatch, MarkedTransport(MARKED, status=400), log)

    response, evidence = _relay(False, "relay-marked-400")

    assert response.status_code == 400
    assert evidence["outcome_event"]["outcome"] == "error"
    details = evidence["outcome_event"]["details"]
    assert details["omniroute_anomalies"] == ["cache_hit", "emergency_fallback"]
    receipts = _receipts(log)
    assert receipts and all(
        row["omniroute_anomalies"] == ["cache_hit", "emergency_fallback"] for row in receipts
    )
    assert LEAK_SENTINEL not in json.dumps(evidence)


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streamed"])
def test_relay_keeps_repeated_markers_fail_closed_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stream: bool
) -> None:
    log = tmp_path / "verdict-decisions.jsonl"
    repeated = [("X-OmniRoute-Cache", "MISS"), ("X-OmniRoute-Cache", "HIT")]
    if not stream:
        repeated.insert(0, ("content-type", "application/json"))
    _logging_app(monkeypatch, MarkedTransport(repeated, stream=stream), log)

    response, evidence = _relay(stream, f"relay-repeated-{stream}")

    assert response.status_code == 200
    assert evidence["outcome_event"]["details"]["omniroute_anomalies"] == ["cache_hit"]
    assert [row["omniroute_anomalies"] for row in _receipts(log)] == [["cache_hit"]]


@pytest.mark.parametrize("status", [200, 400, 503])
def test_markers_do_not_change_status_body_or_retry_decisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    observed: dict[str, tuple[int, bytes, int]] = {}
    for name, headers in (("clean", CLEAN), ("marked", MARKED)):
        # Each run gets its own health cache: a failed run's feedback would
        # otherwise narrow admission for the next run and confound the comparison.
        monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / f"{name}-health-cache.json"))
        transport = MarkedTransport(headers, status=status)
        _logging_app(monkeypatch, transport, tmp_path / f"{name}-decisions.jsonl")
        response, _ = _relay(False, f"relay-compare-{name}-{status}")
        observed[name] = (response.status_code, response.content, _inference_requests(transport))
    assert observed["marked"] == observed["clean"]
