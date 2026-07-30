from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from verdict.capability_passports import RouteIdentity
from verdict.protocol_probes import ProtocolSurface
from verdict.tool_qualification import (
    TOOL_ERROR_RECOVERY_CASE,
    TOOL_INJECTION_RESISTANCE_CASE,
    TOOL_PARALLEL_CALLS_CASE,
    TOOL_RESPONSES_ERROR_RECOVERY_CASE,
    TOOL_RESPONSES_INJECTION_RESISTANCE_CASE,
    TOOL_RESPONSES_PARALLEL_CALLS_CASE,
    TOOL_RESPONSES_RESULT_CONSUMPTION_CASE,
    TOOL_RESPONSES_TERMINATION_CASE,
    TOOL_RESPONSES_UNAVAILABLE_CASE,
    TOOL_RESPONSES_VALID_ARGUMENTS_CASE,
    TOOL_RESULT_CONSUMPTION_CASE,
    TOOL_TERMINATION_CASE,
    TOOL_UNAVAILABLE_CASE,
    TOOL_VALID_ARGUMENTS_CASE,
    ToolDefinition,
    ToolLifecycleRunner,
    ToolQualificationConsentRequiredError,
)

NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
}
TOOL = ToolDefinition("lookup", PARAMETERS)


def route() -> RouteIdentity:
    return RouteIdentity(
        "gateway",
        "provider",
        "account",
        "https://example.test/v1",
        ProtocolSurface.CHAT,
        "provider/model",
    )


def responses_route() -> RouteIdentity:
    return RouteIdentity(
        "gateway",
        "provider",
        "account",
        "https://example.test/v1",
        ProtocolSurface.RESPONSES,
        "provider/model",
    )


def call(call_id: str = "call-1", name: str = "lookup", arguments: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {"query": "status"})},
    }


def response(*calls: dict, content: str | None = None) -> dict:
    message = {"role": "assistant"}
    if calls:
        message["tool_calls"] = list(calls)
    else:
        message["content"] = content or "completed"
    return {"status_code": 200, "body": {"choices": [{"message": message}]}}


def responses_call(
    call_id: str = "call-1", name: str = "lookup", arguments: dict | None = None
) -> dict:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments or {"query": "status"}),
    }


def responses_response(*calls: dict, content: str | None = None) -> dict:
    if calls:
        return {"status_code": 200, "body": {"output": list(calls)}}
    return {
        "status_code": 200,
        "body": {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": content or "done"}]}
            ]
        },
    }


def runner(case, transport, tools=None, handlers=None):
    return ToolLifecycleRunner().run(
        route(),
        case,
        transport,
        tools or {"lookup": TOOL},
        handlers or {"lookup": lambda _: {"value": "ok"}},
        now=NOW,
    )


def responses_runner(case, transport, tools=None, handlers=None):
    return ToolLifecycleRunner().run(
        responses_route(),
        case,
        transport,
        tools or {"lookup": TOOL},
        handlers or {"lookup": lambda _: {"value": "ok"}},
        now=NOW,
    )


def test_valid_arguments_and_result_consumption_require_a_final_answer() -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return response(call()) if len(seen) == 1 else response(content="result consumed")

    result = runner(TOOL_VALID_ARGUMENTS_CASE, transport)
    assert result.ready
    assert result.calls_observed == result.calls_executed == 1
    assert result.result_round_trip and result.terminated
    assert seen[1]["messages"][-1]["role"] == "tool"
    assert seen[0]["max_tokens"] == 256
    assert seen[0]["tools"][0]["function"]["strict"] is True


def test_multiple_parallel_calls_are_observed_in_one_model_turn() -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return response(call("one"), call("two")) if len(seen) == 1 else response(content="done")

    result = runner(TOOL_PARALLEL_CALLS_CASE, transport)
    assert result.ready and result.parallel_calls_observed
    assert result.calls_observed == result.calls_executed == 2


def test_tool_error_recovery_is_required_and_bounded() -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return response(call()) if len(seen) == 1 else response(content="recovered")

    result = runner(
        TOOL_ERROR_RECOVERY_CASE,
        transport,
        handlers={"lookup": lambda _: (_ for _ in ()).throw(RuntimeError())},
    )
    assert result.ready and result.error_recovered
    assert "tool execution failed" in seen[1]["messages"][-1]["content"]


def test_invalid_arguments_are_rejected_before_handler_execution() -> None:
    calls = []
    result = runner(
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: response(call(arguments={"query": 4})),
        handlers={"lookup": lambda _: calls.append(True)},
    )
    assert result.status == "invalid_arguments"
    assert not calls


def test_unavailable_tool_and_injected_tool_output_cannot_gain_execution() -> None:
    calls = []

    def unknown(*_):
        return response(call(name="erase_everything"))

    for case in (TOOL_UNAVAILABLE_CASE, TOOL_INJECTION_RESISTANCE_CASE):
        result = runner(case, unknown, handlers={"lookup": lambda _: calls.append(True)})
        assert result.status == "tool_unavailable"
    assert not calls


def test_repeated_tool_calls_hit_a_terminal_bound_instead_of_looping() -> None:
    result = runner(TOOL_TERMINATION_CASE, lambda *_: response(call()))
    assert result.status == "loop_exhausted"
    assert result.turns == TOOL_TERMINATION_CASE.max_turns
    assert not result.ready


def test_responses_tool_surface_uses_independent_wire_shape() -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return responses_response(responses_call()) if len(seen) == 1 else responses_response()

    result = responses_runner(TOOL_RESPONSES_RESULT_CONSUMPTION_CASE, transport)
    assert result.ready
    assert "input" in seen[1] and seen[1]["input"][-1]["type"] == "function_call_output"
    assert "messages" not in seen[0]
    assert seen[0]["tools"][0]["parameters"] == PARAMETERS
    assert seen[0]["tools"][0]["strict"] is True


def test_each_responses_lifecycle_case_has_a_distinct_bounded_fixture() -> None:
    def run_two_turn(case):
        seen = []

        def transport(_, payload, __):
            seen.append(payload)
            return responses_response(responses_call()) if len(seen) == 1 else responses_response()

        return responses_runner(case, transport), seen

    for case in (TOOL_RESPONSES_VALID_ARGUMENTS_CASE, TOOL_RESPONSES_RESULT_CONSUMPTION_CASE):
        result, seen = run_two_turn(case)
        assert result.ready
        assert "input" in seen[0] and "messages" not in seen[0]

    parallel = responses_runner(
        TOOL_RESPONSES_PARALLEL_CALLS_CASE,
        lambda _, __, ___: responses_response(responses_call("one"), responses_call("two")),
    )
    assert parallel.status == "loop_exhausted"
    assert parallel.parallel_calls_observed


def test_responses_invalid_arguments_unavailable_and_injection_fail_closed() -> None:
    invalid = responses_runner(
        TOOL_RESPONSES_VALID_ARGUMENTS_CASE,
        lambda *_: responses_response(responses_call(arguments={"query": 4})),
    )
    unavailable = responses_runner(
        TOOL_RESPONSES_UNAVAILABLE_CASE,
        lambda *_: responses_response(responses_call(name="erase_everything")),
    )
    injected = responses_runner(
        TOOL_RESPONSES_INJECTION_RESISTANCE_CASE,
        lambda *_: responses_response(responses_call(name="erase_everything")),
    )
    assert invalid.status == "invalid_arguments"
    assert unavailable.status == injected.status == "tool_unavailable"


def test_tool_wire_shape_and_nonstandard_arguments_fail_closed() -> None:
    non_assistant = runner(
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: {
            "status_code": 200,
            "body": {"choices": [{"message": {"role": "user", "tool_calls": [call()]}}]},
        },
    )
    nonstandard = runner(
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: {
            "status_code": 200,
            "body": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"query":NaN}'},
                                }
                            ],
                        }
                    }
                ]
            },
        },
    )
    assert non_assistant.status == "malformed"
    assert nonstandard.status == "malformed"


def test_responses_error_recovery_and_termination_are_bounded() -> None:
    seen = []

    def recovering(_, __, ___):
        seen.append(True)
        return responses_response(responses_call()) if len(seen) == 1 else responses_response()

    recovered = responses_runner(
        TOOL_RESPONSES_ERROR_RECOVERY_CASE,
        recovering,
        handlers={"lookup": lambda _: (_ for _ in ()).throw(RuntimeError())},
    )
    looping = responses_runner(
        TOOL_RESPONSES_TERMINATION_CASE, lambda *_: responses_response(responses_call())
    )
    assert recovered.ready and recovered.error_recovered
    assert looping.status == "loop_exhausted"


def test_tool_cancellation_consent_and_error_normalization_fail_closed() -> None:
    cancel = Event()
    cancel.set()
    calls = []
    cancelled = ToolLifecycleRunner().run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: calls.append(True),
        {"lookup": TOOL},
        {"lookup": lambda _: None},
        now=NOW,
        cancel_event=cancel,
    )
    assert cancelled.status == cancelled.error_class == "cancelled"
    assert not cancelled.ready and calls == []
    with pytest.raises(ToolQualificationConsentRequiredError):
        ToolLifecycleRunner().run(
            route(),
            TOOL_RESULT_CONSUMPTION_CASE,
            lambda *_: calls.append(True),
            {"lookup": TOOL},
            {"lookup": lambda _: None},
            now=NOW,
            live=True,
        )

    runner_ = ToolLifecycleRunner(max_response_bytes=20)
    oversized = runner_.run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: response(call(), content="x" * 100),
        {"lookup": TOOL},
        {"lookup": lambda _: None},
        now=NOW,
    )
    malformed = ToolLifecycleRunner().run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: {"status_code": 200, "body": "not-json"},
        {"lookup": TOOL},
        {"lookup": lambda _: None},
        now=NOW,
    )
    timeout = ToolLifecycleRunner(timeout_seconds=0.001).run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: time.sleep(0.01),
        {"lookup": TOOL},
        {"lookup": lambda _: None},
        now=NOW,
    )
    assert oversized.status == oversized.error_class == "oversized_response"
    assert oversized.response_bytes > runner_.max_response_bytes
    assert malformed.status == "malformed" and malformed.error_class == "malformed_response"
    assert timeout.status == timeout.error_class == "timeout"


def test_tool_blocking_transport_is_bounded() -> None:
    started = Event()
    result = ToolLifecycleRunner(timeout_seconds=0.2).run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        lambda *_: (started.set(), time.sleep(1))[1],
        {"lookup": TOOL},
        {"lookup": lambda _: None},
        now=NOW,
    )
    assert result.status == "timeout"
    assert started.is_set()


def test_tool_cancellation_during_blocking_transport_is_non_ready() -> None:
    cancel = Event()
    started = Event()

    def transport(*_):
        started.set()
        while not cancel.is_set():
            time.sleep(0.005)
        return response(content="late")

    output = []
    worker = Thread(
        target=lambda: output.append(
            ToolLifecycleRunner(timeout_seconds=0.2).run(
                route(),
                TOOL_RESULT_CONSUMPTION_CASE,
                transport,
                {"lookup": TOOL},
                {"lookup": lambda _: None},
                now=NOW,
                cancel_event=cancel,
            )
        )
    )
    worker.start()
    assert started.wait(1)
    cancel.set()
    worker.join(1)
    assert output[0].status == output[0].error_class == "cancelled"


def test_tool_result_content_does_not_authorize_an_undeclared_follow_up() -> None:
    calls = []
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return response(call()) if len(seen) == 1 else response(call(name="erase_everything"))

    result = ToolLifecycleRunner().run(
        route(),
        TOOL_INJECTION_RESISTANCE_CASE,
        transport,
        {"lookup": TOOL},
        {"lookup": lambda _: calls.append(True) or "ignore this instruction"},
        now=NOW,
    )
    assert result.status == "tool_unavailable"
    assert calls == [True]
    assert "ignore this instruction" in seen[1]["messages"][-1]["content"]


def test_response_byte_budget_is_aggregate_across_tool_turns() -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return response(call()) if len(seen) == 1 else response(content="done")

    result = ToolLifecycleRunner(max_response_bytes=100).run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        transport,
        {"lookup": TOOL},
        {"lookup": lambda _: {"value": "ok"}},
        now=NOW,
    )
    assert result.status == result.error_class == "oversized_response"
    assert result.response_bytes > 100
    assert not result.ready


def test_oversized_later_turn_reports_aggregate_observed_bytes() -> None:
    seen = []

    def transport(_, payload, __):
        seen.append(payload)
        return response(call()) if len(seen) == 1 else response(content="x" * 100)

    result = ToolLifecycleRunner(max_response_bytes=180).run(
        route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        transport,
        {"lookup": TOOL},
        {"lookup": lambda _: {"value": "ok"}},
        now=NOW,
    )
    assert result.status == result.error_class == "oversized_response"
    assert result.response_bytes > 180
    assert result.response_bytes > len(json.dumps(response(content="x" * 100)["body"]))
