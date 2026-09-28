"""Tests for BOD-203 AC6: per-attempt token usage in orchestration."""

from __future__ import annotations

import json
from typing import Any

import pytest

from verdict.orchestration.contracts import AttemptUsage, RunEvent, WorkerTerminal
from verdict.orchestration.executors import PrimeHeadlessExecutor
from verdict.orchestration.receipt import _node_record

# ── helpers ──────────────────────────────────────────────────────────

ROUTE = "omniroute/test-model"


def _assistant_payload(
    text: str = "PONG",
    model: str = ROUTE,
    provider: str = "omniroute",
    stop_reason: str = "stop",
    usage: dict[str, Any] | None = None,
    **extra: object,
) -> str:
    msg: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "openai-completions",
        "provider": provider,
        "model": model,
        "stopReason": stop_reason,
    }
    if usage is not None:
        msg["usage"] = usage
    msg.update(extra)
    doc = {"messages": [{"role": "user", "content": "hi"}, msg]}
    return json.dumps(doc)


def _make_executor() -> PrimeHeadlessExecutor:
    return PrimeHeadlessExecutor(prime_bin="/dev/null", provider="omniroute")


def _interpret(stdout: str, route: str = ROUTE) -> WorkerTerminal:
    ex = _make_executor()
    return ex._interpret(stdout=stdout, stderr="", returncode=0, route_id=route, duration=1.0)


def _terminal_event(seq: int = 1, ok: bool = True, usage: dict[str, Any] | None = None) -> RunEvent:
    data: dict[str, Any] = {
        "ok": ok,
        "route_id": ROUTE,
        "reported_model": ROUTE,
        "duration_seconds": 1.5,
        "attempt": 1,
    }
    if usage is not None:
        data["usage"] = usage
    return RunEvent(seq=seq, at="2026-01-01T00:00:00Z", type="terminal", node_id="n1", data=data)


def _dispatch_event(seq: int = 0) -> RunEvent:
    return RunEvent(
        seq=seq,
        at="2026-01-01T00:00:00Z",
        type="dispatch",
        node_id="n1",
        data={"route_id": ROUTE, "provider": "omniroute", "attempt": 1},
    )


# ── AttemptUsage dataclass ───────────────────────────────────────────


class TestAttemptUsage:
    def test_defaults(self) -> None:
        u = AttemptUsage()
        assert u.input_tokens is None
        assert u.output_tokens is None
        assert u.cost_usd is None
        assert u.tokens_source is None

    def test_with_values(self) -> None:
        u = AttemptUsage(
            input_tokens=10, output_tokens=5, cost_usd=0.001, tokens_source="prime_stdout"
        )
        assert u.input_tokens == 10
        assert u.output_tokens == 5
        assert u.cost_usd == 0.001

    def test_frozen(self) -> None:
        u = AttemptUsage(input_tokens=10)
        with pytest.raises(AttributeError):
            u.input_tokens = 20  # type: ignore[misc]


# ── WorkerTerminal.usage field ───────────────────────────────────────


class TestWorkerTerminalUsage:
    def test_default_none(self) -> None:
        t = WorkerTerminal(ok=True)
        assert t.usage is None

    def test_with_usage(self) -> None:
        u = AttemptUsage(input_tokens=100, output_tokens=50)
        t = WorkerTerminal(ok=True, usage=u)
        assert t.usage is u
        assert t.usage.input_tokens == 100

    def test_backward_compatible(self) -> None:
        """Old code that doesn't pass usage= still works."""
        t = WorkerTerminal(ok=False, error="test")
        assert t.usage is None


# ── Executor _extract_usage ──────────────────────────────────────────


class TestExtractUsage:
    def test_prime_stdout_keys(self) -> None:
        """Prime stdout uses input/output keys."""
        u = PrimeHeadlessExecutor._extract_usage({"usage": {"input": 10, "output": 5}})
        assert u is not None
        assert u.input_tokens == 10
        assert u.output_tokens == 5
        assert u.cost_usd is None
        assert u.tokens_source == "prime_stdout"

    def test_openai_keys(self) -> None:
        """OpenAI uses prompt_tokens/completion_tokens."""
        u = PrimeHeadlessExecutor._extract_usage(
            {"usage": {"prompt_tokens": 20, "completion_tokens": 15}}
        )
        assert u is not None
        assert u.input_tokens == 20
        assert u.output_tokens == 15

    def test_input_preferred_over_prompt_tokens(self) -> None:
        """When both key styles present, input/output wins."""
        u = PrimeHeadlessExecutor._extract_usage(
            {"usage": {"input": 10, "output": 5, "prompt_tokens": 99, "completion_tokens": 99}}
        )
        assert u is not None
        assert u.input_tokens == 10
        assert u.output_tokens == 5

    def test_with_cost(self) -> None:
        u = PrimeHeadlessExecutor._extract_usage(
            {"usage": {"input": 10, "output": 5, "cost_usd": 0.003}}
        )
        assert u is not None
        assert u.cost_usd == 0.003

    def test_cost_key_variant(self) -> None:
        u = PrimeHeadlessExecutor._extract_usage(
            {"usage": {"input": 10, "output": 5, "cost": 0.007}}
        )
        assert u is not None
        assert u.cost_usd == 0.007

    def test_no_usage_key(self) -> None:
        assert PrimeHeadlessExecutor._extract_usage({"role": "assistant"}) is None

    def test_none_assistant(self) -> None:
        assert PrimeHeadlessExecutor._extract_usage(None) is None

    def test_empty_usage_dict(self) -> None:
        assert PrimeHeadlessExecutor._extract_usage({"usage": {}}) is None

    def test_usage_not_dict(self) -> None:
        assert PrimeHeadlessExecutor._extract_usage({"usage": "invalid"}) is None


# ── Executor _interpret with usage ───────────────────────────────────


class TestInterpretUsage:
    def test_success_with_usage(self) -> None:
        stdout = _assistant_payload(usage={"input": 100, "output": 50})
        t = _interpret(stdout)
        assert t.ok is True
        assert t.usage is not None
        assert t.usage.input_tokens == 100
        assert t.usage.output_tokens == 50

    def test_success_without_usage(self) -> None:
        stdout = _assistant_payload(usage=None)
        t = _interpret(stdout)
        assert t.ok is True
        assert t.usage is None

    def test_error_with_usage(self) -> None:
        """Even failed attempts carry usage when the assistant reported it."""
        stdout = _assistant_payload(
            usage={"input": 10, "output": 2}, errorMessage="429 rate limited"
        )
        t = _interpret(stdout)
        assert t.ok is False
        assert t.usage is not None
        assert t.usage.input_tokens == 10

    def test_no_assistant_no_usage(self) -> None:
        """When there's no assistant message, usage is None."""
        ex = _make_executor()
        t = ex._interpret(stdout="garbage", stderr="", returncode=1, route_id=ROUTE, duration=1.0)
        assert t.ok is False
        assert t.usage is None


# ── Receipt _node_record carries usage ───────────────────────────────


class TestNodeRecordUsage:
    def test_attempt_row_carries_usage(self) -> None:
        usage = {
            "input_tokens": 200,
            "output_tokens": 80,
            "cost_usd": 0.01,
            "tokens_source": "prime_stdout",
        }
        events = [_dispatch_event(0), _terminal_event(1, ok=True, usage=usage)]
        record = _node_record("n1", "implement", events)
        attempts = record["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["usage"] == usage

    def test_attempt_row_no_usage(self) -> None:
        """Old events without usage key produce no usage in the attempt row."""
        events = [_dispatch_event(0), _terminal_event(1, ok=True, usage=None)]
        record = _node_record("n1", "implement", events)
        attempts = record["attempts"]
        assert len(attempts) == 1
        assert "usage" not in attempts[0]

    def test_failed_attempt_with_usage(self) -> None:
        usage = {
            "input_tokens": 50,
            "output_tokens": 10,
            "cost_usd": None,
            "tokens_source": "prime_stdout",
        }
        events = [_dispatch_event(0), _terminal_event(1, ok=False, usage=usage)]
        record = _node_record("n1", "implement", events)
        assert record["attempts"][0]["usage"] == usage
        assert record["attempts"][0]["outcome"] == "failure"


# ── Digest verification (receipt SHA-256) ────────────────────────────


class TestDigestWithUsage:
    """The receipt digest is a SHA-256 of events.jsonl on disk.

    Adding usage to terminal events changes the file content, but old files
    without usage still hash correctly. The key invariant: verify_run_receipt
    compares stored digest with a fresh hash of the same file. As long as
    events.jsonl is not mutated after receipt creation, verification passes.
    """

    def test_event_roundtrip_with_usage(self) -> None:
        """RunEvent with usage data survives to_dict/from_dict."""
        data = {"ok": True, "usage": {"input_tokens": 100, "output_tokens": 50}}
        ev = RunEvent(seq=1, at="2026-01-01T00:00:00Z", type="terminal", node_id="n1", data=data)
        restored = RunEvent.from_dict(ev.to_dict())
        assert restored.data["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_event_without_usage_unchanged(self) -> None:
        """Old events without usage serialize identically."""
        data = {"ok": True, "duration_seconds": 1.0}
        ev = RunEvent(seq=1, at="2026-01-01T00:00:00Z", type="terminal", node_id="n1", data=data)
        d = ev.to_dict()
        assert "usage" not in d["data"]
        restored = RunEvent.from_dict(d)
        assert restored.data == data
