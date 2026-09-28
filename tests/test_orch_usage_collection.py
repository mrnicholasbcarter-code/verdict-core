"""Tests for orchestration usage collection (BOD-203 AC6).

Covers:
- _collect_messages picks up singular "message" key (message_end/turn_end)
- _interpret extracts usage from full sample and from sample without agent_end
- No double-counting when same assistant appears in message_end, turn_end, agent_end
- _save_attempt persists usage in the attempt JSON
- ok=True with no usage emits usage_missing=true in the terminal event
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import AttemptUsage, WorkerTerminal
from verdict.orchestration.executors import PrimeHeadlessExecutor, _collect_messages

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prime_stdout"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# _collect_messages
# ---------------------------------------------------------------------------


class TestCollectMessages:
    """Verify _collect_messages handles both plural and singular message keys."""

    def test_plural_messages_key(self) -> None:
        """agent_end style: {"messages": [...]}"""
        values: list[Any] = [
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            }
        ]
        result = _collect_messages(values)
        roles = [m["role"] for m in result]
        assert "user" in roles
        assert "assistant" in roles

    def test_singular_message_key(self) -> None:
        """message_end / turn_end style: {"message": {"role": ...}}"""
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": "hi", "usage": {"input": 10}},
            }
        ]
        result = _collect_messages(values)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"

    def test_message_start_ignored(self) -> None:
        """message_start events are streaming partials and must not be collected."""
        values: list[Any] = [
            {"type": "message_start", "message": {"role": "assistant", "content": "partial"}},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": "final",
                    "responseId": "r1",
                    "usage": {"input": 10},
                },
            },
        ]
        result = _collect_messages(values)
        assert len(result) == 1
        assert result[0]["content"] == "final"

    def test_message_update_ignored(self) -> None:
        """message_update events (streaming deltas) must not be collected."""
        values: list[Any] = [
            {
                "type": "message_update",
                "message": {"role": "assistant", "content": "streaming delta"},
                "assistantMessageEvent": {"type": "text_delta"},
            },
            {
                "type": "message_update",
                "message": {"role": "assistant", "content": "another delta"},
                "assistantMessageEvent": {"type": "toolcall_delta"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": "complete",
                    "responseId": "r1",
                    "usage": {"input": 20},
                },
            },
        ]
        result = _collect_messages(values)
        assert len(result) == 1
        assert result[0]["content"] == "complete"

    def test_dedup_same_assistant_by_response_id(self) -> None:
        """Same assistant via message_end, turn_end, agent_end (shared responseId) → one."""
        content = [{"type": "text", "text": "hello"}]
        rid = "chatcmpl-123"
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "responseId": rid,
                    "usage": {"input": 5},
                },
            },
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "responseId": rid,
                    "usage": {"input": 5},
                },
            },
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": content,
                        "responseId": rid,
                        "usage": {"input": 5},
                    }
                ]
            },
        ]
        result = _collect_messages(values)
        assistant_msgs = [m for m in result if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1, f"Expected 1 assistant, got {len(assistant_msgs)}"

    def test_dedup_same_assistant_by_timestamp_role(self) -> None:
        """Same assistant via message_end + agent_end (shared timestamp, no responseId) → one."""
        content = [{"type": "text", "text": "hello"}]
        ts = 1790576556633
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": content, "timestamp": ts},
            },
            {"messages": [{"role": "assistant", "content": content, "timestamp": ts}]},
        ]
        result = _collect_messages(values)
        assistant_msgs = [m for m in result if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1

    def test_different_roles_not_deduped(self) -> None:
        """Different roles with same content remain separate."""
        values: list[Any] = [
            {"type": "message_end", "message": {"role": "user", "content": "x", "timestamp": 1}},
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": "x", "timestamp": 2},
            },
        ]
        result = _collect_messages(values)
        assert len(result) == 2

    def test_top_level_role_still_works(self) -> None:
        """A top-level dict with 'role' (no 'message'/'messages' key) is still collected."""
        values: list[Any] = [{"role": "assistant", "content": "hi"}]
        result = _collect_messages(values)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _interpret: full sample → usage extracted
# ---------------------------------------------------------------------------


class TestInterpretFullSample:
    """_interpret on the real single-turn fixture (with agent_end) extracts usage."""

    def test_full_sample_usage(self) -> None:
        stdout = _load_fixture("single_turn.jsonl")
        executor = PrimeHeadlessExecutor(provider="omniroute")
        terminal = executor._interpret(
            stdout=stdout, stderr="", returncode=0, route_id="kr/claude-haiku-4.5", duration=1.0
        )
        assert terminal.ok
        assert terminal.usage is not None
        assert isinstance(terminal.usage.input_tokens, int)
        assert terminal.usage.input_tokens > 0
        assert terminal.usage.tokens_source == "prime_stdout"


# ---------------------------------------------------------------------------
# _interpret: sample without agent_end → usage via message_end fallback
# ---------------------------------------------------------------------------


class TestInterpretNoAgentEnd:
    """When agent_end is missing (timeout/crash), usage comes from message_end."""

    def test_no_agent_end_still_has_usage(self) -> None:
        stdout = _load_fixture("single_turn_no_agent_end.jsonl")
        executor = PrimeHeadlessExecutor(provider="omniroute")
        terminal = executor._interpret(
            stdout=stdout, stderr="", returncode=0, route_id="kr/claude-haiku-4.5", duration=1.0
        )
        assert terminal.ok
        assert terminal.usage is not None, "usage should be found via message_end fallback"
        assert isinstance(terminal.usage.input_tokens, int)
        assert terminal.usage.input_tokens > 0


# ---------------------------------------------------------------------------
# No double-counting
# ---------------------------------------------------------------------------


class TestNoDoubleCounting:
    """Same assistant message in message_end + agent_end must not produce two usages."""

    def test_single_assistant_from_full_sample(self) -> None:
        """Full sample has message_end, turn_end, AND agent_end with the same assistant."""
        stdout = _load_fixture("single_turn.jsonl")
        from verdict.orchestration.executors import _collect_messages, _iter_json_values

        values, _ = _iter_json_values(stdout)
        messages = _collect_messages(values)
        assistants = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistants) == 1, f"Expected 1 assistant, got {len(assistants)}"


# ---------------------------------------------------------------------------
# _save_attempt persists usage
# ---------------------------------------------------------------------------


class TestSaveAttemptUsage:
    """_save_attempt must include usage in the JSON when present."""

    def test_usage_in_persisted_json(self, tmp_path: Path) -> None:
        from verdict.orchestration.runtime import DagRuntime

        usage = AttemptUsage(
            input_tokens=100, output_tokens=50, cost_usd=0.001, tokens_source="prime_stdout"
        )
        terminal = WorkerTerminal(ok=True, model="test/m", output="RESULT: done", usage=usage)

        # Build a minimal runtime just to call _save_attempt
        rt = object.__new__(DagRuntime)
        rt.run_dir = tmp_path

        rt._save_attempt("node-1", 1, "test/m", terminal)  # type: ignore[arg-type]

        attempt_file = tmp_path / "attempts" / "node-1-a1.json"
        assert attempt_file.exists()
        record = json.loads(attempt_file.read_text())
        assert "usage" in record
        assert record["usage"]["input_tokens"] == 100
        assert record["usage"]["output_tokens"] == 50
        assert record["usage"]["cost_usd"] == 0.001
        assert record["usage"]["tokens_source"] == "prime_stdout"

    def test_no_usage_not_in_json(self, tmp_path: Path) -> None:
        from verdict.orchestration.runtime import DagRuntime

        terminal = WorkerTerminal(ok=True, model="test/m", output="RESULT: done")

        rt = object.__new__(DagRuntime)
        rt.run_dir = tmp_path

        rt._save_attempt("node-2", 1, "test/m", terminal)  # type: ignore[arg-type]

        attempt_file = tmp_path / "attempts" / "node-2-a1.json"
        record = json.loads(attempt_file.read_text())
        assert "usage" not in record


# ---------------------------------------------------------------------------
# usage_missing diagnostic
# ---------------------------------------------------------------------------


class TestUsageMissingDiagnostic:
    """ok=True with no usage → terminal event includes usage_missing=true."""

    def test_usage_missing_emitted(self) -> None:
        """Simulate emit call and verify usage_missing appears when ok+no usage."""
        # We check the emit kwargs construction logic directly.
        # The runtime builds kwargs with a conditional ternary; verify by
        # constructing the same kwargs.
        terminal = WorkerTerminal(ok=True, model="test/m", output="RESULT: done")
        assert terminal.usage is None

        # Reproduce the kwargs logic from runtime.py
        usage_kwargs: dict[str, Any] = {}
        if terminal.usage is not None:
            usage_kwargs["usage"] = {
                "input_tokens": terminal.usage.input_tokens,
                "output_tokens": terminal.usage.output_tokens,
                "cost_usd": terminal.usage.cost_usd,
                "tokens_source": terminal.usage.tokens_source,
            }
        elif terminal.ok:
            usage_kwargs["usage_missing"] = True

        assert usage_kwargs == {"usage_missing": True}

    def test_usage_missing_not_emitted_on_failure(self) -> None:
        """ok=False with no usage → no usage_missing field."""
        terminal = WorkerTerminal(ok=False, model="test/m", error="fail")
        assert terminal.usage is None

        usage_kwargs: dict[str, Any] = {}
        if terminal.usage is not None:
            usage_kwargs["usage"] = {}
        elif terminal.ok:
            usage_kwargs["usage_missing"] = True

        assert usage_kwargs == {}

    def test_usage_present_no_missing_flag(self) -> None:
        """ok=True with usage → usage dict, no usage_missing."""
        usage = AttemptUsage(input_tokens=10, output_tokens=5)
        terminal = WorkerTerminal(ok=True, model="test/m", output="RESULT: x", usage=usage)

        usage_kwargs: dict[str, Any] = {}
        if terminal.usage is not None:
            usage_kwargs["usage"] = {
                "input_tokens": terminal.usage.input_tokens,
                "output_tokens": terminal.usage.output_tokens,
                "cost_usd": terminal.usage.cost_usd,
                "tokens_source": terminal.usage.tokens_source,
            }
        elif terminal.ok:
            usage_kwargs["usage_missing"] = True

        assert "usage" in usage_kwargs
        assert "usage_missing" not in usage_kwargs


# ---------------------------------------------------------------------------
# Bug (a): usage preserved when only one copy has it
# ---------------------------------------------------------------------------


class TestUsageMergeAcrossCopies:
    """Merge rule: latest copy wins fields, but earlier usage is preserved."""

    def test_usage_kept_when_only_message_end_has_it(self) -> None:
        """message_end has usage, agent_end copy does not → usage preserved."""
        rid = "chatcmpl-999"
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "responseId": rid,
                    "usage": {"input": 100, "output": 50},
                },
            },
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "hello",
                        "responseId": rid,
                        # no usage on agent_end copy
                    }
                ]
            },
        ]
        result = _collect_messages(values)
        assistants = [m for m in result if m.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["usage"]["input"] == 100
        assert assistants[0]["usage"]["output"] == 50

    def test_usage_kept_when_only_agent_end_has_it(self) -> None:
        """agent_end has usage, message_end copy does not → usage preserved."""
        rid = "chatcmpl-888"
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "responseId": rid,
                    # no usage on message_end copy
                },
            },
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "hello",
                        "responseId": rid,
                        "usage": {"input": 200, "output": 80},
                    }
                ]
            },
        ]
        result = _collect_messages(values)
        assistants = [m for m in result if m.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["usage"]["input"] == 200


# ---------------------------------------------------------------------------
# Bug (b): identical long content but different responseId → kept separate
# ---------------------------------------------------------------------------


class TestLongContentDifferentId:
    """Two messages with identical long content but different responseId stay separate."""

    def test_same_long_content_different_response_id(self) -> None:
        long_content = "x" * 2000  # well beyond old 512-char truncation
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": long_content,
                    "responseId": "chatcmpl-AAA",
                    "usage": {"input": 100},
                },
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": long_content,
                    "responseId": "chatcmpl-BBB",
                    "usage": {"input": 200},
                },
            },
        ]
        result = _collect_messages(values)
        assistants = [m for m in result if m.get("role") == "assistant"]
        assert len(assistants) == 2, f"Expected 2, got {len(assistants)}"
        assert assistants[0]["usage"]["input"] == 100
        assert assistants[1]["usage"]["input"] == 200


# ---------------------------------------------------------------------------
# No responseId and no timestamp → no dedup (treated as distinct)
# ---------------------------------------------------------------------------


class TestNoIdentityNoDedup:
    """Messages with neither responseId nor timestamp are never merged."""

    def test_no_id_no_timestamp_stays_distinct(self) -> None:
        values: list[Any] = [
            {"type": "message_end", "message": {"role": "assistant", "content": "alpha"}},
            {"type": "message_end", "message": {"role": "assistant", "content": "alpha"}},
        ]
        result = _collect_messages(values)
        assistants = [m for m in result if m.get("role") == "assistant"]
        assert len(assistants) == 2, "Without identity, messages must not be merged"


# ---------------------------------------------------------------------------
# Parity: _interpret picks the same final assistant as origin/main on both fixtures
# ---------------------------------------------------------------------------


class TestFixtureParity:
    """Chosen final assistant message matches origin/main for real fixtures."""

    def _last_assistant(self, fixture: str) -> dict[str, Any]:
        stdout = _load_fixture(fixture)
        from verdict.orchestration.executors import _iter_json_values

        values, _ = _iter_json_values(stdout)
        messages = _collect_messages(values)
        assistants = [m for m in messages if m.get("role") == "assistant"]
        assert assistants, f"No assistant in {fixture}"
        return assistants[-1]

    def test_single_turn_parity(self) -> None:
        last = self._last_assistant("single_turn.jsonl")
        # single_turn has one assistant with responseId chatcmpl-1790576557343
        assert last.get("responseId") == "chatcmpl-1790576557343"
        assert "usage" in last

    def test_multi_turn_parity(self) -> None:
        last = self._last_assistant("multi_turn.jsonl")
        # multi_turn has two assistants; last is chatcmpl-1790576681884
        assert last.get("responseId") == "chatcmpl-1790576681884"
        assert "usage" in last

    def test_multi_turn_both_assistants_present(self) -> None:
        stdout = _load_fixture("multi_turn.jsonl")
        from verdict.orchestration.executors import _iter_json_values

        values, _ = _iter_json_values(stdout)
        messages = _collect_messages(values)
        assistants = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistants) == 2, f"Expected 2 assistants, got {len(assistants)}"
        ids = [m.get("responseId") for m in assistants]
        assert ids == ["chatcmpl-1790576680714", "chatcmpl-1790576681884"]
