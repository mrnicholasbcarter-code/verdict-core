"""Tests for BOD-203 AC6: total per-attempt token usage across all assistant turns.

Verifies that _extract_usage sums usage over all DISTINCT assistant messages
(deduped by _collect_messages via responseId), and that the `turns` field
counts assistant messages contributing usage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import AttemptUsage
from verdict.orchestration.executors import (
    PrimeHeadlessExecutor,
    _collect_messages,
    _iter_json_values,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prime_stdout"


def _load_fixture(name: str) -> list[dict[str, Any]]:
    text = (FIXTURES / name).read_text()
    values, _ = _iter_json_values(text)
    return _collect_messages(values)


# ---------------------------------------------------------------- single turn


class TestSingleTurnUsage:
    """Single-turn fixture: one assistant message, usage unchanged."""

    def test_single_turn_totals(self) -> None:
        messages = _load_fixture("single_turn.jsonl")
        usage = PrimeHeadlessExecutor._extract_usage(messages)
        assert usage is not None
        assert usage.input_tokens == 8588
        assert usage.output_tokens == 1
        assert usage.cost_usd == 0.0
        assert usage.turns == 1
        assert usage.tokens_source == "prime_stdout"

    def test_single_turn_no_agent_end(self) -> None:
        messages = _load_fixture("single_turn_no_agent_end.jsonl")
        usage = PrimeHeadlessExecutor._extract_usage(messages)
        assert usage is not None
        assert usage.input_tokens == 8588
        assert usage.output_tokens == 1
        assert usage.turns == 1


# ---------------------------------------------------------------- multi turn


class TestMultiTurnUsage:
    """Multi-turn fixture: two assistant messages, usage summed."""

    def test_multi_turn_sums_input_and_output(self) -> None:
        messages = _load_fixture("multi_turn.jsonl")
        usage = PrimeHeadlessExecutor._extract_usage(messages)
        assert usage is not None
        # Turn 1: input=8684, output=0
        # Turn 2: input=8714, output=4
        assert usage.input_tokens == 8684 + 8714
        assert usage.output_tokens == 0 + 4
        assert usage.turns == 2

    def test_multi_turn_cost_summed_when_all_present(self) -> None:
        messages = _load_fixture("multi_turn.jsonl")
        usage = PrimeHeadlessExecutor._extract_usage(messages)
        assert usage is not None
        # Both messages have cost.total = 0
        assert usage.cost_usd == 0.0


# ---------------------------------------------------------------- cost_usd None when partial


class TestCostNoneWhenPartial:
    """If one assistant message is missing cost, cost_usd must be None."""

    def test_missing_cost_yields_none(self) -> None:
        messages = [
            {
                "role": "assistant",
                "responseId": "a1",
                "usage": {"input": 100, "output": 10, "cost": {"total": 0.05}},
                "content": "hello",
                "model": "m",
                "provider": "p",
                "stopReason": "stop",
            },
            {
                "role": "assistant",
                "responseId": "a2",
                "usage": {"input": 200, "output": 20},
                "content": "world",
                "model": "m",
                "provider": "p",
                "stopReason": "stop",
            },
        ]
        usage = PrimeHeadlessExecutor._extract_usage(messages)
        assert usage is not None
        assert usage.input_tokens == 300
        assert usage.output_tokens == 30
        assert usage.cost_usd is None  # one message missing cost -> None
        assert usage.turns == 2


# ---------------------------------------------------------------- duplicates counted once


class TestDuplicatesCounted:
    """Duplicate copies of one message (same responseId) counted once after dedup."""

    def test_deduped_copies(self) -> None:
        msg = {
            "role": "assistant",
            "responseId": "dup-1",
            "usage": {"input": 500, "output": 50, "cost": {"total": 0.01}},
            "content": [{"type": "text", "text": "answer"}],
            "model": "kr/claude-haiku-4.5",
            "provider": "omniroute",
            "stopReason": "stop",
        }
        # Simulate raw Prime output: message_end + turn_end + agent_end all carry copies
        raw_values: list[dict[str, Any]] = [
            {"type": "message_end", "message": dict(msg)},
            {"type": "turn_end", "message": dict(msg)},
            {"type": "agent_end", "messages": [dict(msg)]},
        ]
        messages = _collect_messages(raw_values)
        assistants = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistants) == 1, "dedup should collapse copies with same responseId"
        usage = PrimeHeadlessExecutor._extract_usage(messages)
        assert usage is not None
        assert usage.input_tokens == 500
        assert usage.output_tokens == 50
        assert usage.turns == 1


# ---------------------------------------------------------------- no assistant messages


class TestNoAssistantMessages:
    """_extract_usage returns None when there are no assistant messages."""

    def test_empty_list(self) -> None:
        assert PrimeHeadlessExecutor._extract_usage([]) is None

    def test_only_user_messages(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        assert PrimeHeadlessExecutor._extract_usage(messages) is None

    def test_assistant_without_usage(self) -> None:
        messages = [{"role": "assistant", "content": "hi", "responseId": "x"}]
        assert PrimeHeadlessExecutor._extract_usage(messages) is None


# ---------------------------------------------------------------- AttemptUsage turns field


class TestAttemptUsageTurns:
    """The turns field is optional and backward-compatible."""

    def test_default_none(self) -> None:
        usage = AttemptUsage(input_tokens=100, output_tokens=10)
        assert usage.turns is None

    def test_explicit_turns(self) -> None:
        usage = AttemptUsage(input_tokens=100, output_tokens=10, turns=3)
        assert usage.turns == 3


# ---------------------------------------------------------------- _interpret integration


class TestInterpretUsesAllTurns:
    """_interpret must sum usage from all assistant turns, not just the last."""

    def test_interpret_multi_turn(self) -> None:
        stdout = (FIXTURES / "multi_turn.jsonl").read_text()
        executor = PrimeHeadlessExecutor()
        terminal = executor._interpret(
            stdout=stdout, stderr="", returncode=0, route_id="kr/claude-haiku-4.5", duration=1.0
        )
        assert terminal.usage is not None
        assert terminal.usage.input_tokens == 8684 + 8714
        assert terminal.usage.output_tokens == 4
        assert terminal.usage.turns == 2

    def test_interpret_single_turn(self) -> None:
        stdout = (FIXTURES / "single_turn.jsonl").read_text()
        executor = PrimeHeadlessExecutor()
        terminal = executor._interpret(
            stdout=stdout, stderr="", returncode=0, route_id="kr/claude-haiku-4.5", duration=1.0
        )
        assert terminal.usage is not None
        assert terminal.usage.input_tokens == 8588
        assert terminal.usage.output_tokens == 1
        assert terminal.usage.turns == 1
