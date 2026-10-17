"""BOD-430: bare API-token rule in ``verdict.shared_memory.redact_secrets``.

Same word-bounded pattern set as ``verdict_memory`` (BOD-423, PR #906). Fake tokens
are assembled at runtime so no secret-shaped literal sits in the source. Covers the
Hermes adapter's local JSONL outbox, where the live smoke on 2026-10-16 stored a bare
``sk-`` token raw.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from verdict import harness_hermes_memory as hm
from verdict.shared_memory import redact_secrets


def _tail(n: int) -> str:
    return ("FAKE" + "0123456789abcdefghijKLMNOPQRST" * 4)[:n]


_SK = "s" + "k-"

TOKENS: dict[str, Callable[[], str]] = {
    "sk_alnum": lambda: _SK + _tail(48),
    "sk_proj": lambda: _SK + "proj-" + "test_" + _tail(40),
    "sk_ant": lambda: _SK + "ant-api03-" + _tail(40),
    "sk_or": lambda: _SK + "or-v1-" + _tail(40),
    "sk_svcacct": lambda: _SK + "svcacct-" + _tail(40),
    "xai": lambda: "x" + "ai-" + _tail(80),
    "groq": lambda: "g" + "sk_" + _tail(52),
    "hf": lambda: "h" + "f_" + _tail(34),
    "gitlab": lambda: "gl" + "pat-" + _tail(20),
    "stripe_sk_test": lambda: "s" + "k_test_" + _tail(24),
    "stripe_sk_live": lambda: "s" + "k_live_" + _tail(24),
    "stripe_rk_test": lambda: "r" + "k_test_" + _tail(24),
    "stripe_rk_live": lambda: "r" + "k_live_" + _tail(24),
}

BENIGN = (
    "task-runner-configuration-loader",
    "risk-assessment-dashboard-widget",
    "desk-booking-service-for-office-2026",
    "disk-usage-reporting-module-v2",
    _SK + "learn-preprocessing-pipeline-notes",
    "task-" + "runner2026configuration0123456789",
    "desk-" + "0123456789abcdefghij2026",
    "disk-" + "proj-0123456789abcdefghij",
    "risk_test_" + "0123456789abcdefghij0123",
    "ofxai-" + "0123456789abcdefghij" * 3,
    "abgsk_" + "0123456789abcdefghij" * 3,
    "nohf_" + "0123456789abcdefghij" * 2,
    "myglpat-" + "0123456789abcdefghij",
)


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_bare_tokens_are_redacted(name: str) -> None:
    token = TOKENS[name]()
    for text in (f"use key {token} now.", json.dumps({"k": token}), f"`{token}`"):
        out = redact_secrets(text)
        assert token not in out
        assert "FAKE" not in out
        assert "[REDACTED]" in out


@pytest.mark.parametrize("text", BENIGN)
def test_benign_words_are_not_mangled(text: str) -> None:
    assert redact_secrets(f"see {text} here") == f"see {text} here"


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_redaction_is_idempotent(name: str) -> None:
    once = redact_secrets(f"a {TOKENS[name]()} b")
    assert redact_secrets(once) == once


def test_existing_rules_still_apply() -> None:
    out = redact_secrets("api_key=abc123 Bearer abcdefghijklmnop https://u:p@example.com/x")
    assert out == "api_key=[REDACTED] Bearer [REDACTED] https://[REDACTED]@example.com/x"


def test_hermes_local_outbox_never_stores_bare_tokens(tmp_path: Path) -> None:
    """Regression for the 2026-10-16 live redaction smoke (bare token stored raw)."""
    tokens = [make() for make in TOKENS.values()]
    outbox_path = tmp_path / "outbox.jsonl"
    bridge = hm.HermesMemoryBridge(
        provider=None, outbox=hm.BoundedJsonlOutbox(outbox_path), project="proj-a"
    )
    payload = {
        "session_id": "20261017_000000_abc123",
        "turn_id": "t-1",
        "assistant_response": "keys: " + " ".join(tokens) + " and task-runner-config ok",
        "model": "fake-model",
        "cwd": "/home/USER/dev/project-a",
    }
    response = bridge.handle("post_llm_call", payload)
    assert response.status == "queued"
    raw = outbox_path.read_text(encoding="utf-8")
    assert raw.strip(), "the capture must still be queued, redacted"
    for token in tokens:
        assert token not in raw
    assert "FAKE" not in raw
    assert "task-runner-config ok" in raw
