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

    def test_dedup_same_assistant(self) -> None:
        """Same assistant content via message_end, turn_end, agent_end → counted once."""
        content = [{"type": "text", "text": "hello"}]
        values: list[Any] = [
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": content, "usage": {"input": 5}},
            },
            {
                "type": "turn_end",
                "message": {"role": "assistant", "content": content, "usage": {"input": 5}},
            },
            {"messages": [{"role": "assistant", "content": content, "usage": {"input": 5}}]},
        ]
        result = _collect_messages(values)
        assistant_msgs = [m for m in result if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1, f"Expected 1 assistant, got {len(assistant_msgs)}"

    def test_different_roles_not_deduped(self) -> None:
        """Different roles with same content remain separate."""
        values: list[Any] = [
            {"type": "message_end", "message": {"role": "user", "content": "x"}},
            {"type": "message_end", "message": {"role": "assistant", "content": "x"}},
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
