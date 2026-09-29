"""Deterministic tests for _invoke_bounded completion-time judgment.

Uses an injectable _clock keyword to avoid real sleeps.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from verdict.capability_passports import RouteIdentity
from verdict.protocol_probes import ProtocolSurface
from verdict.tool_qualification import (
    TOOL_RESULT_CONSUMPTION_CASE,
    ToolDefinition,
    ToolLifecycleRunner,
    _invoke_bounded,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
}
TOOL = ToolDefinition("lookup", PARAMETERS)
NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)


def _route() -> RouteIdentity:
    return RouteIdentity(
        "gateway",
        "provider",
        "account",
        "https://example.test/v1",
        ProtocolSurface.CHAT,
        "provider/model",
    )


# ---------------------------------------------------------------------------
# (a) Deterministic: completion AFTER deadline → timeout
#
# Strategy: inject a fake clock that returns a small value for the main thread
# (so deadline = small, remaining always > 0 from the main thread's view) and
# a large value from the worker thread (completion_time[0] > deadline).
# The callable itself returns immediately so future.done() is quickly true.
# ---------------------------------------------------------------------------


def test_invoke_bounded_late_completion_is_timeout() -> None:
    """A result whose completion time exceeds the deadline must be a timeout."""
    from verdict.tool_qualification import _TransportTimedOutError

    _main = threading.current_thread()
    _main_calls: list[int] = [0]

    def _fake_clock() -> float:
        if threading.current_thread() is _main:
            _main_calls[0] += 1
            # Call 1: deadline computation → deadline = 0.0 + 0.5 = 0.5
            # Subsequent calls (remaining): return 0.1 → remaining = 0.4 > 0
            # so main loop never self-expires via the remaining path.
            return 0.0 if _main_calls[0] == 1 else 0.1
        # Worker thread records completion at t=1.0 >> deadline=0.5 → timeout.
        return 1.0

    def _fast(*_args: object) -> str:
        return "done"

    with pytest.raises(_TransportTimedOutError):
        _invoke_bounded(_fast, (), timeout_seconds=0.5, cancel_event=None, _clock=_fake_clock)


# ---------------------------------------------------------------------------
# (b) Deterministic: completion BEFORE deadline → result returned
# ---------------------------------------------------------------------------


def test_invoke_bounded_early_completion_returns_result() -> None:
    """A callable that finishes before the deadline must return its value."""

    # Real clock, generous deadline: can never expire in practice.
    def _fast(*_args: object) -> str:
        return "expected_value"

    result = _invoke_bounded(_fast, (), timeout_seconds=10.0, cancel_event=None)
    assert result == "expected_value"


# ---------------------------------------------------------------------------
# (b2) End-to-end: ToolLifecycleRunner with a fast transport does NOT timeout
# ---------------------------------------------------------------------------


def _response_body(content: str = "ok") -> dict:
    return {
        "status_code": 200,
        "body": {"choices": [{"message": {"role": "assistant", "content": content}}]},
    }


def test_runner_fast_transport_does_not_timeout() -> None:
    """ToolLifecycleRunner must not produce a timeout when transport is fast."""

    def _transport(*_args: object) -> dict:
        return _response_body("completed")

    obs = ToolLifecycleRunner(timeout_seconds=5.0).run(
        _route(),
        TOOL_RESULT_CONSUMPTION_CASE,
        _transport,
        {"lookup": TOOL},
        {"lookup": lambda _: None},
        now=NOW,
    )
    assert obs.status != "timeout", f"Unexpected timeout; got status={obs.status!r}"
