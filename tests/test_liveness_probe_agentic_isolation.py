"""The 1-token liveness probe is chat liveness only: it never grants or clears agentic proof.

Captured 2026-11-11 from the operator gateway (3 calls, ``max_tokens: 1``): each route answered
HTTP 200 with a well-formed assistant message cut off by the token limit and empty or null text.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import conn, make_ladder, row
from tests.test_subagent_selection import _Response
from verdict.admit_prove_confirm import record_dispatch_observation
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.subagent_selection import HealthResult, candidates_from_inventory, openai_health_probe

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
PROOF_AT = NOW - timedelta(hours=3)
ROUTE = "groq/openai/gpt-oss-20b"

# Verbatim bodies from the capture (ids shortened to what the checks read).
GEMINI = b'{"object":"chat.completion","model":"gemini-3-flash","choices":[{"index":0,"finish_reason":"length","message":{"role":"assistant","content":""}}],"usage":{"prompt_tokens":81,"completion_tokens":0,"total_tokens":81}}'
GPT_OSS = b'{"object":"chat.completion","model":"openai/gpt-oss-20b","choices":[{"index":0,"finish_reason":"length","message":{"role":"assistant","content":""},"logprobs":null}],"usage":{"prompt_tokens":75,"completion_tokens":1,"total_tokens":76}}'
BEAM = b'{"object":"chat.completion","model":"beam-free","choices":[{"index":0,"finish_reason":"length","message":{"role":"assistant","refusal":null,"content":null},"logprobs":null}],"usage":{"prompt_tokens":50,"completion_tokens":1,"total_tokens":51}}'


class _HeaderResponse(_Response):
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(status, body)
        self.headers = headers


def _headers(provider: str, model: str) -> dict[str, str]:
    return {
        "x-omniroute-cache": "MISS",
        "x-omniroute-cache-hit": "false",
        "x-omniroute-compression": "off; source=off",
        "x-omniroute-decision": f"strategy=single; provider={provider}; latency_ms=262",
        "x-omniroute-model": model,
        "x-omniroute-provider": provider,
    }


def _probe(route: str, body: bytes, headers: dict[str, str]) -> HealthResult:
    candidate = candidates_from_inventory(
        [{"id": route, "owned_by": route.split("/")[0], "pricing": {"input": 0, "output": 0}}],
        [f"omniroute/{route}"],
    )[0]
    probe = openai_health_probe(opener=lambda request, timeout: _HeaderResponse(200, body, headers))
    return probe(candidate)


# --- liveness rule: a well-formed reply cut off by the limit is alive, and flagged ------------


@pytest.mark.parametrize(
    ("route", "body", "provider", "model", "flag"),
    [
        (
            "antigravity/gemini-3-flash",
            GEMINI,
            "antigravity",
            "gemini-3-flash",
            "zero_output_tokens",
        ),
        ("groq/openai/gpt-oss-20b", GPT_OSS, "groq", "openai/gpt-oss-20b", "empty_content"),
        ("opencode-go/beam-free", BEAM, "opencode-go", "beam-free", "empty_content"),
    ],
)
def test_captured_empty_content_replies_count_as_alive_and_flagged(
    route: str, body: bytes, provider: str, model: str, flag: str
) -> None:
    result = _probe(route, body, _headers(provider, model))

    assert result.healthy is True and result.category == "healthy" and result.status_code == 200
    assert "empty_content" in result.error_message
    assert flag in result.error_message


def test_zero_output_tokens_is_flagged_even_when_text_is_null() -> None:
    body = json.dumps(
        {
            "choices": [
                {"finish_reason": "length", "message": {"role": "assistant", "content": None}}
            ],
            "usage": {"completion_tokens": 0},
        }
    ).encode()
    result = _probe("groq/openai/gpt-oss-20b", body, _headers("groq", "openai/gpt-oss-20b"))
    assert result.healthy is True and "zero_output_tokens" in result.error_message


def test_empty_content_without_a_verified_identity_is_still_malformed() -> None:
    assert _probe(ROUTE, GPT_OSS, {}) == HealthResult(False, "malformed_response", 200)


def test_empty_content_from_a_different_served_model_is_still_malformed() -> None:
    result = _probe(ROUTE, GPT_OSS, _headers("groq", "llama-3.3-70b"))
    assert result == HealthResult(False, "malformed_response", 200)


def test_empty_content_from_a_cached_reply_is_not_alive() -> None:
    headers = {**_headers("groq", "openai/gpt-oss-20b"), "x-omniroute-cache": "HIT"}
    result = _probe(ROUTE, GPT_OSS, headers)
    assert result.healthy is False and result.category == "omniroute_cache_hit"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"object":"list"}',
        b'{"choices":[]}',
        b'{"choices":[{"finish_reason":"length","message":{"role":"user","content":""}}]}',
        b'{"choices":[{"finish_reason":"content_filter","message":{"role":"assistant","content":""}}]}',
        # operator-specified: empty text with finish_reason stop is malformed, not alive
        b'{"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":""}}]}',
        b'{"choices":[{"message":{"role":"assistant","content":""}}]}',
        b'{"choices":[{"finish_reason":"length"}]}',
    ],
)
def test_genuinely_malformed_replies_stay_malformed_response(body: bytes) -> None:
    result = _probe(ROUTE, body, _headers("groq", "openai/gpt-oss-20b"))
    assert result == HealthResult(False, "malformed_response", 200)


def test_a_reply_with_text_is_alive_exactly_as_before() -> None:
    body = b'{"choices":[{"message":{"role":"assistant","content":"OK"}}]}'
    assert _probe(ROUTE, body, {}) == HealthResult(True, "healthy", 200)


# --- agentic isolation: liveness failures never clear tool proof -------------------------------


def _proven_cache(tmp_path: Path) -> HealthCache:
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record(
        ROUTE,
        ProbeResult(CATEGORY_OK, True, True, probe_class="agentic", agentic_ok=True),
        PROOF_AT,
    )
    cache.save()
    return cache


def _agentic(cache: HealthCache) -> tuple[bool, datetime | None]:
    entry = HealthCache(cache.path).entry(ROUTE)
    assert entry is not None
    return entry.agentic_ok, entry.agentic_checked_at


def test_liveness_failure_keeps_agentic_proof_and_still_marks_chat_unhealthy(
    tmp_path: Path,
) -> None:
    cache = _proven_cache(tmp_path)

    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
        liveness_only=True,
    )

    assert _agentic(cache) == (True, PROOF_AT)
    entry = HealthCache(cache.path).entry(ROUTE)
    assert entry is not None and entry.healthy is False and entry.category == "malformed_response"
    assert entry.chat_ok is False


def test_non_liveness_failure_still_clears_agentic_proof(tmp_path: Path) -> None:
    """The global behaviour is unchanged: malformed_response is not exempt for other callers."""
    cache = _proven_cache(tmp_path)

    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
    )

    assert _agentic(cache)[0] is False


def test_exempt_categories_are_not_widened() -> None:
    """Pins the exact set the operator said must not grow: malformed_response stays out."""
    import inspect

    from verdict.orchestration import health_cache

    source = inspect.getsource(health_cache.HealthCache.record)
    block = source.split("preserve_agentic = (", 1)[1].split("if preserve_agentic", 1)[0]
    exempt = {
        name
        for name in ("TIMEOUT", "UPSTREAM", "RATE_LIMITED", "AUTH", "PAYMENT", "PERMISSION")
        if f"CATEGORY_{name}" in block
    }
    assert exempt == {"TIMEOUT", "UPSTREAM", "RATE_LIMITED", "AUTH", "PAYMENT", "PERMISSION"}
    assert "MALFORMED" not in block.upper() and "malformed" not in block


def test_liveness_failure_does_not_touch_session_agentic_proof(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record_agentic_evidence(ROUTE, passed=True, at=PROOF_AT, source="session_evidence")
    cache.save()

    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
        liveness_only=True,
    )

    entry = HealthCache(cache.path).entry(ROUTE)
    assert entry is not None
    assert (entry.session_agentic_ok, entry.session_agentic_at) == (True, PROOF_AT)


def test_liveness_failure_on_an_unproven_route_does_not_invent_proof(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health-cache.json")

    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
        liveness_only=True,
    )

    entry = HealthCache(cache.path).entry(ROUTE)
    assert entry is not None and entry.agentic_ok is False and entry.agentic_checked_at is None


def test_the_ladder_records_its_probe_as_liveness_only(tmp_path: Path) -> None:
    """The 12:00:34Z failure: the ladder's 1-token probe must not wipe the route's tool proof."""
    cache = _proven_cache(tmp_path)
    rows: list[dict[str, Any]] = [
        row(ROUTE, owned_by="groq", pricing={"input": 0.0, "output": 0.0})
    ]
    ladder, _ = make_ladder(
        tmp_path,
        rows,
        [conn("groq", auth="apikey", plan="free", free_only=True)],
        health_cache=cache,
    )

    ladder._record_health(
        ROUTE, HealthResult(False, "malformed_response", 200), NOW, provider="groq"
    )

    assert _agentic(cache) == (True, PROOF_AT)


def test_the_worker_launch_probe_records_its_failure_as_liveness_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker_runtime runs the same 1-token probe; its failure must not clear tool proof either."""
    import asyncio

    import verdict.worker_runtime as worker_runtime
    from tests.test_worker_admission import DEAD, SpyAdapter, _fresh_host_set
    from verdict.subagent_selection import HealthCache as SelectionCache
    from verdict.worker_runtime import WorkerController, WorkerTask

    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record(
        DEAD, ProbeResult(CATEGORY_OK, True, True, probe_class="agentic", agentic_ok=True), PROOF_AT
    )
    cache.save()
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(cache.path))
    seen: list[bool] = []
    real = worker_runtime.record_dispatch_observation

    def spy(*args: Any, **kwargs: Any) -> None:
        seen.append(bool(kwargs.get("liveness_only")))
        real(
            *args, **kwargs, health_cache_path=cache.path
        ) if "health_cache_path" not in kwargs else real(*args, **kwargs)

    monkeypatch.setattr(worker_runtime, "record_dispatch_observation", spy)

    class Malformed:
        def __call__(self, candidate: Any) -> HealthResult:
            return HealthResult(False, "malformed_response", 200)

    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=[row(DEAD)],
        prime_selectors=[f"omniroute/{DEAD}"],
        probe=Malformed(),
        adapter=SpyAdapter(),
        cache=SelectionCache(tmp_path / "selection.json"),
        now=lambda: NOW,
        admitted=_fresh_host_set(tmp_path),
        require_admission=True,
    )
    asyncio.run(ctrl.run("task"))

    assert seen and all(seen)
    entry = HealthCache(cache.path).entry(DEAD)
    assert entry is not None and entry.agentic_ok is True


# --- Q1 review findings: only a null/empty/whitespace text with finish_reason length is alive ---

_ID = _headers("groq", "openai/gpt-oss-20b")


@pytest.mark.parametrize(
    "payload",
    [
        # 1. refusal string, no content key
        {
            "choices": [
                {
                    "message": {"role": "assistant", "refusal": "I cannot help"},
                    "finish_reason": "length",
                }
            ]
        },
        # 1b. refusal next to empty content
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "", "refusal": "no"},
                    "finish_reason": "length",
                }
            ]
        },
        # 2. top-level error object next to an empty assistant message
        {
            "error": {"message": "upstream failed"},
            "choices": [
                {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
            ],
        },
        # 3. tool_calls only
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
                    "finish_reason": "length",
                }
            ]
        },
        # 3b. legacy function_call
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "function_call": {"name": "f"},
                    },
                    "finish_reason": "length",
                }
            ]
        },
        # 5. first choice is a user message, second is an empty assistant message
        {
            "choices": [
                {"message": {"role": "user", "content": "x"}, "finish_reason": "length"},
                {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"},
            ]
        },
        # 5b. two choices, both empty assistant messages
        {
            "choices": [
                {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"},
                {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"},
            ]
        },
        # 6. assistant message with no content key at all and nothing else to explain it
        # (no content key is not null: only an explicit null or empty string is accepted)
        {"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]},
        # empty text with finish_reason stop
        {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]},
        # non-string content shapes
        {"choices": [{"message": {"role": "assistant", "content": []}, "finish_reason": "length"}]},
        {"choices": [{"message": {"role": "assistant", "content": 0}, "finish_reason": "length"}]},
        # null error key is still an error key only when truthy; a populated one is rejected
        {
            "choices": [
                {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
            ],
            "error": "boom",
        },
    ],
    ids=[
        "refusal-no-content",
        "refusal-with-empty-content",
        "top-level-error",
        "tool-calls-only",
        "function-call-only",
        "user-then-empty-assistant",
        "two-empty-assistant-choices",
        "no-content-key",
        "empty-with-stop",
        "list-content",
        "numeric-content",
        "error-string",
    ],
)
def test_reply_shapes_that_are_not_a_truncated_empty_completion_stay_malformed(
    payload: dict[str, Any],
) -> None:
    result = _probe(ROUTE, json.dumps(payload).encode(), _ID)
    assert result == HealthResult(False, "malformed_response", 200), payload


@pytest.mark.parametrize("content", [None, "", "   ", "\n\t "])
def test_null_empty_or_whitespace_text_with_length_is_alive_and_flagged(content: Any) -> None:
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "length"}
        ]
    }
    result = _probe(ROUTE, json.dumps(payload).encode(), _ID)
    assert result.healthy is True and result.status_code == 200
    assert "empty_content" in result.error_message


def test_null_error_and_null_refusal_keys_do_not_disqualify_a_truncated_reply() -> None:
    """The captured beam-free body carries ``refusal: null``; null error/tool_calls are fine."""
    payload = {
        "error": None,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "refusal": None,
                    "tool_calls": None,
                    "function_call": None,
                },
                "finish_reason": "length",
            }
        ],
    }
    assert _probe(ROUTE, json.dumps(payload).encode(), _ID).healthy is True


def test_whitespace_text_is_not_inference_output_so_it_is_only_alive_through_the_flagged_path() -> (
    None
):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "  "}, "finish_reason": "length"}]
    }
    result = _probe(ROUTE, json.dumps(payload).encode(), _ID)
    assert (result.healthy, result.error_message) == (True, "empty_content")


# --- tool_ok: a failed liveness probe keeps it, and no reader selects on it alone ----------------


def test_liveness_failure_keeps_tool_ok_but_marks_the_route_unhealthy(tmp_path: Path) -> None:
    cache = _proven_cache(tmp_path)

    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
        liveness_only=True,
    )

    entry = HealthCache(cache.path).entry(ROUTE)
    assert entry is not None
    assert (entry.tool_ok, entry.agentic_ok) == (True, True)
    assert (entry.healthy, entry.chat_ok) == (False, False)
    # unhealthy: never fresh or stale, so every reader that requires a usable state skips it
    assert entry.state_at(NOW + timedelta(seconds=1)) in {"negative", "unprobed"}


def test_non_liveness_failure_still_clears_tool_ok(tmp_path: Path) -> None:
    cache = _proven_cache(tmp_path)
    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
    )
    entry = HealthCache(cache.path).entry(ROUTE)
    assert entry is not None and entry.tool_ok is False


def test_a_route_kept_tool_ok_by_liveness_is_not_selected_by_the_readers(tmp_path: Path) -> None:
    """An unhealthy entry with tool_ok kept must not look like a usable coding worker."""
    from verdict.prove_at_rest import status_report

    cache = _proven_cache(tmp_path)
    record_dispatch_observation(
        ROUTE,
        healthy=False,
        observed_at=NOW,
        failure_category="malformed_response",
        health_cache_path=cache.path,
        liveness_only=True,
    )
    reloaded = HealthCache(cache.path)
    report = status_report(reloaded, now=NOW + timedelta(seconds=1))

    assert report["top_healthy_coding_workers"] == []
    assert reloaded.lookup(ROUTE, NOW + timedelta(seconds=1)).healthy is False
