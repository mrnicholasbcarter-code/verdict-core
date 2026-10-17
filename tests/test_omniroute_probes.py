"""Failing specs for discovery probe judging and state machine (spec v6.1 sections 2-3)."""

from __future__ import annotations

from verdict.omniroute_pipeline.probes import (
    ProbeOutcome,
    judge_chat,
    judge_tool,
    seed_usable_context,
)
from verdict.omniroute_pipeline.state import ModelState, should_probe, transition


def _chat(content: str, *, rid: str = "chatcmpl-1", finish: str = "stop") -> dict[str, object]:
    return {
        "id": rid,
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"total_tokens": 5},
    }


def test_chat_pong_passes() -> None:
    assert judge_chat(200, _chat("PONG")) is ProbeOutcome.PASS


def test_fake_200_id_and_topup_body() -> None:
    assert judge_chat(200, _chat("PONG", rid="chatcmpl-fake-123")) is ProbeOutcome.FAIL_FAKE
    assert judge_chat(200, _chat("Please Top-Up your balance")) is ProbeOutcome.FAIL_FAKE
    assert judge_chat(200, _chat("recharge required")) is ProbeOutcome.FAIL_FAKE
    empty = {"id": "x", "choices": [], "usage": {"total_tokens": 0}}
    assert judge_chat(200, empty) is ProbeOutcome.FAIL_FAKE


def test_tool_as_text_fails() -> None:
    as_text = _chat('{"name": "get_time", "arguments": {}}')
    assert judge_tool(200, as_text) is ProbeOutcome.FAIL
    good = {
        "id": "c",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {"tool_calls": [{"function": {"name": "get_time", "arguments": "{}"}}]},
            }
        ],
        "usage": {"total_tokens": 9},
    }
    assert judge_tool(200, good) is ProbeOutcome.PASS
    bad_args = {
        "id": "c",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [{"function": {"name": "get_time", "arguments": "{oops"}}]
                },
            }
        ],
        "usage": {"total_tokens": 9},
    }
    assert judge_tool(200, bad_args) is ProbeOutcome.FAIL


def test_context_seeded_from_existing_needles() -> None:
    seeds = seed_usable_context()
    assert seeds["nvidia/z-ai/glm-5.3"].usable_context == 825_000
    assert seeds["kc/nvidia/nemotron-3.5-super-vl:free"].usable_context == 700_000
    assert seeds["kc/reflection/beam-free"].usable_context == 570_000
    assert all(s.measured_at is not None for s in seeds.values())


def test_shutdown_ids_never_probed() -> None:
    state = transition(ModelState.CATALOGED, http_status=410, error_code="model_shutdown")
    assert state is ModelState.SHUTDOWN
    assert should_probe(state) is False
    assert should_probe(ModelState.UNVERIFIED_FREE) is False
    assert should_probe(ModelState.POLICY_REJECTED) is False


def test_billing_failures_go_needs_account() -> None:
    assert transition(ModelState.ELIGIBLE, http_status=402) is ModelState.NEEDS_ACCOUNT
    assert transition(ModelState.ELIGIBLE, probe=ProbeOutcome.FAIL_FAKE) is ModelState.NEEDS_ACCOUNT
    assert transition(ModelState.CATALOGED, http_status=404) is ModelState.NEEDS_SYNC
