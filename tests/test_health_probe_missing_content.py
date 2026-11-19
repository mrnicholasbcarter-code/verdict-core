"""The 1-token liveness probe and a truncated reply whose message has no ``content`` key.

Captured 2026-11-19 from the operator gateway's call log (2 calls to ``cerebras/gpt-oss-120b``,
``max_tokens: 1``): HTTP 200, one choice, ``finish_reason: "length"``, a message holding only
``role``, 1 completion token. The fixture keeps that shape; ids and timings are redacted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_liveness_probe_agentic_isolation import BEAM, GEMINI, GPT_OSS, _headers, _probe
from verdict.subagent_selection import HealthResult

ROUTE = "cerebras/gpt-oss-120b"
FIXTURE = Path(__file__).parent / "fixtures" / "health_probe" / "missing-content-length.json"
MALFORMED = HealthResult(False, "malformed_response", 200)


def _captured() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


def _run(payload: Any, headers: dict[str, str] | None = None) -> HealthResult:
    served = _headers("cerebras", "gpt-oss-120b") if headers is None else headers
    return _probe(ROUTE, json.dumps(payload).encode(), served)


def _with_message(**extra: Any) -> dict[str, Any]:
    payload = _captured()
    payload["choices"][0]["message"].update(extra)
    return payload


def test_fixture_is_the_captured_shape_and_carries_no_account_data() -> None:
    payload = _captured()
    assert list(payload["choices"][0]["message"]) == ["role"]
    text = FIXTURE.read_text(encoding="utf-8")
    assert "/home/" not in text and "chatcmpl" not in text and "Bearer" not in text


def test_captured_missing_content_reply_is_alive_under_its_own_flag() -> None:
    result = _run(_captured())

    assert result == HealthResult(True, "healthy", 200, error_message="missing_content")


def test_missing_content_with_zero_output_tokens_keeps_both_flags() -> None:
    payload = _captured()
    payload["usage"]["completion_tokens"] = 0

    result = _run(payload)

    assert (result.healthy, result.error_message) == (True, "missing_content,zero_output_tokens")


def test_missing_content_that_stopped_on_its_own_is_malformed() -> None:
    payload = _captured()
    payload["choices"][0]["finish_reason"] = "stop"

    assert _run(payload) == MALFORMED


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_calls": [{"id": "c", "type": "function", "function": {"name": "f"}}]},
        {"tool_calls": []},
        {"refusal": "I cannot help with that."},
        {"refusal": ""},
        {"function_call": {"name": "f", "arguments": "{}"}},
    ],
    ids=["tool_calls", "empty_tool_calls", "refusal", "empty_refusal", "function_call"],
)
def test_missing_content_with_a_refusal_or_tool_payload_is_malformed(extra: dict[str, Any]) -> None:
    assert _run(_with_message(**extra)) == MALFORMED


def test_missing_content_with_an_error_object_is_malformed() -> None:
    payload = _captured()
    payload["error"] = {"message": "upstream failed", "type": "server_error"}

    assert _run(payload) == MALFORMED


@pytest.mark.parametrize("count", [0, 2])
def test_missing_content_needs_exactly_one_choice(count: int) -> None:
    payload = _captured()
    choice = payload["choices"][0]
    payload["choices"] = [copy.deepcopy(choice) for _ in range(count)]

    assert _run(payload) == MALFORMED


@pytest.mark.parametrize(
    "headers",
    [{}, _headers("cerebras", "llama-3.3-70b"), _headers("groq", "gpt-oss-120b")],
    ids=["no_identity", "other_model", "other_provider"],
)
def test_missing_content_without_a_verified_identity_matches_the_empty_content_result(
    headers: dict[str, str],
) -> None:
    # Same category the existing empty-content rule gives for an unverified identity.
    assert _probe("groq/openai/gpt-oss-20b", GPT_OSS, {}) == MALFORMED
    assert _run(_captured(), headers) == MALFORMED


def test_missing_content_from_a_cached_reply_is_not_alive() -> None:
    headers = {**_headers("cerebras", "gpt-oss-120b"), "x-omniroute-cache": "HIT"}

    result = _run(_captured(), headers)

    assert (result.healthy, result.category) == (False, "omniroute_cache_hit")


@pytest.mark.parametrize("role", ["user", "tool", "system", None, ""])
def test_missing_content_from_a_non_assistant_message_is_malformed(role: str | None) -> None:
    assert _run(_with_message(role=role)) == MALFORMED


@pytest.mark.parametrize(
    "usage",
    [
        "absent",
        None,
        {},
        {"completion_tokens": None},
        {"completion_tokens": "1"},
        {"completion_tokens": 1.0},
        {"completion_tokens": True},
        {"completion_tokens": -1},
    ],
    ids=["absent", "null", "empty", "null_count", "text", "float", "bool", "negative"],
)
def test_missing_content_needs_a_readable_output_token_count(usage: Any) -> None:
    payload = _captured()
    if usage == "absent":
        del payload["usage"]
    else:
        payload["usage"] = usage

    assert _run(payload) == MALFORMED


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reasoning", ["thinking"]),
        ("reasoning", {"text": "thinking"}),
        ("reasoning_content", 0),
        ("reasoning_content", False),
    ],
)
def test_missing_content_with_an_unreadable_reasoning_field_is_malformed(
    key: str, value: Any
) -> None:
    assert _run(_with_message(**{key: value})) == MALFORMED


@pytest.mark.parametrize(("key", "value"), [("reasoning", None), ("reasoning_content", " ")])
def test_missing_content_with_a_blank_reasoning_field_is_still_the_same_shape(
    key: str, value: Any
) -> None:
    assert _run(_with_message(**{key: value})).error_message == "missing_content"


def test_reasoning_text_without_content_is_inference_output_exactly_as_before() -> None:
    # Not the missing-content rule: reasoning text already counted as output before this change.
    result = _run(_with_message(reasoning="The user wants OK."))

    assert result == HealthResult(True, "healthy", 200)


@pytest.mark.parametrize(
    ("route", "body", "provider", "model", "flags"),
    [
        (
            "antigravity/gemini-3-flash",
            GEMINI,
            "antigravity",
            "gemini-3-flash",
            "empty_content,zero_output_tokens",
        ),
        ("groq/openai/gpt-oss-20b", GPT_OSS, "groq", "openai/gpt-oss-20b", "empty_content"),
        ("opencode-go/beam-free", BEAM, "opencode-go", "beam-free", "empty_content"),
    ],
)
def test_present_empty_or_null_content_keeps_its_existing_flags_exactly(
    route: str, body: bytes, provider: str, model: str, flags: str
) -> None:
    result = _probe(route, body, _headers(provider, model))

    assert result == HealthResult(True, "healthy", 200, error_message=flags)


def test_an_empty_content_reply_needs_no_usage_exactly_as_before() -> None:
    body = b'{"choices":[{"finish_reason":"length","message":{"role":"assistant","content":""}}]}'

    result = _probe("groq/openai/gpt-oss-20b", body, _headers("groq", "openai/gpt-oss-20b"))

    assert result == HealthResult(True, "healthy", 200, error_message="empty_content")
