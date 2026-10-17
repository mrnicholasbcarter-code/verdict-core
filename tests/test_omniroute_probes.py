"""Probe judging and the per-model state machine (spec v6.1 sections 2-3)."""

from __future__ import annotations

import pytest

from verdict.omniroute_pipeline.probes import ProbeOutcome, is_fake_200, judge_chat


def _chat(content: str, *, rid: str = "chatcmpl-1", finish: str = "stop") -> dict[str, object]:
    return {
        "id": rid,
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"total_tokens": 5},
    }


def test_chat_pong_passes() -> None:
    assert judge_chat(200, _chat("PONG")) is ProbeOutcome.PASS
    assert judge_chat(200, _chat("cut", finish="length")) is ProbeOutcome.FAIL
    assert judge_chat(500, _chat("PONG")) is ProbeOutcome.FAIL


def test_fake_200_id_and_topup_body() -> None:
    assert judge_chat(200, _chat("PONG", rid="chatcmpl-fake-123")) is ProbeOutcome.FAIL_FAKE
    assert judge_chat(200, _chat("Please Top-Up your balance")) is ProbeOutcome.FAIL_FAKE
    assert judge_chat(200, _chat("recharge required")) is ProbeOutcome.FAIL_FAKE
    assert judge_chat(200, _chat("topup now")) is ProbeOutcome.FAIL_FAKE
    empty = {"id": "x", "choices": [], "usage": {"total_tokens": 0}}
    assert judge_chat(200, empty) is ProbeOutcome.FAIL_FAKE
    assert is_fake_200(_chat("PONG")) is False


def test_tool_as_text_fails() -> None:
    from verdict.omniroute_pipeline.probes import judge_tool

    assert judge_tool(200, _chat('{"name": "get_time", "arguments": {}}')) is ProbeOutcome.FAIL
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
    bad = {
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
    assert judge_tool(200, bad) is ProbeOutcome.FAIL


def test_context_seeded_from_existing_needles() -> None:
    from verdict.omniroute_pipeline.probes import seed_usable_context

    seeds = seed_usable_context()
    assert seeds["nvidia/z-ai/glm-5.3"].usable_context == 825_000
    assert seeds["kc/nvidia/nemotron-3.5-super-vl:free"].usable_context == 700_000
    assert seeds["kc/reflection/beam-free"].usable_context == 570_000
    assert all(s.measured_at is not None for s in seeds.values())


@pytest.mark.xfail(strict=True, raises=ImportError, reason="phase b step 3: state machine")
def test_shutdown_ids_never_probed() -> None:
    from verdict.omniroute_pipeline.state import ModelState, should_probe, transition

    state = transition(ModelState.CATALOGED, http_status=410, error_code="model_shutdown")
    assert state is ModelState.SHUTDOWN
    assert should_probe(state) is False
    assert should_probe(ModelState.UNVERIFIED_FREE) is False
    assert should_probe(ModelState.POLICY_REJECTED) is False


@pytest.mark.xfail(strict=True, raises=ImportError, reason="phase b step 3: state machine")
def test_billing_failures_go_needs_account() -> None:
    from verdict.omniroute_pipeline.state import ModelState, ProviderBlocklist, transition

    blocklist = ProviderBlocklist()
    assert transition(ModelState.ELIGIBLE, http_status=402) is ModelState.NEEDS_ACCOUNT
    assert transition(ModelState.CATALOGED, http_status=404) is ModelState.NEEDS_SYNC
    state = transition(
        ModelState.ELIGIBLE,
        probe=ProbeOutcome.FAIL_FAKE,
        route="ahm/some-model",
        blocklist=blocklist,
    )
    assert state is ModelState.NEEDS_ACCOUNT
    assert blocklist.is_blocked("ahm") is True
    assert blocklist.reason("ahm") == "fail_fake"


def test_probe_policy_needle_and_steps() -> None:
    from verdict.omniroute_pipeline.probes import (
        judge_needle,
        needle_payload,
        next_context_step,
        probe_allowed,
    )

    assert probe_allowed("kc/reflection/beam-free", free_status="free") is True
    assert probe_allowed("agy/gemini-3.8-flash-high", free_status="free") is False
    assert probe_allowed("cc/x", free_status="free") is False
    payload = needle_payload("nvidia/z-ai/glm-5.3", 128_000, "ZQ-7")
    assert "ZQ-7" in str(payload["messages"])
    assert judge_needle(200, _chat("ZQ-7"), "ZQ-7") is ProbeOutcome.PASS
    assert judge_needle(200, _chat("no idea"), "ZQ-7") is ProbeOutcome.FAIL
    assert next_context_step(None, 262_000) == 128_000
    assert next_context_step(570_000, 1_000_000) == 825_000
    assert next_context_step(256_000, 262_000) is None


def test_usable_context_store_seeds_and_records(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from verdict.omniroute_pipeline.context_store import (
        context_due,
        load_usable_context,
        record_usable_context,
    )

    path = tmp_path / "usable-context.json"
    now = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    rows = load_usable_context(path)
    assert rows["nvidia/z-ai/glm-5.3"]["usable_context"] == 825_000
    assert context_due(rows["nvidia/z-ai/glm-5.3"], now) is False
    assert context_due(rows["nvidia/z-ai/glm-5.3"], now + timedelta(days=8)) is True
    assert context_due(None, now) is True
    record_usable_context(path, "x/m", 128_000, now=now, failed_step=256_000)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_usable_context(path)["x/m"]["usable_context"] == 128_000
    assert context_due(load_usable_context(path)["x/m"], now) is False
