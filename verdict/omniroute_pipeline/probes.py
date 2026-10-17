"""Probe judging. Phase a ships the fake-200 guard and the chat judge only."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

_BILLING_MARKERS = ("topup", "top-up", "recharge")


class ProbeOutcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    FAIL_FAKE = "fail_fake"


def is_fake_200(body: object) -> bool:
    """Spec section 2: fake id, billing text in the body, or empty choices with zero usage."""
    if not isinstance(body, Mapping):
        return False
    rid = body.get("id")
    if isinstance(rid, str) and rid.startswith("chatcmpl-fake"):
        return True
    import json

    try:
        text = json.dumps(body, default=str).lower()
    except (TypeError, ValueError):
        text = str(body).lower()
    if any(marker in text for marker in _BILLING_MARKERS):
        return True
    choices = body.get("choices")
    usage = body.get("usage")
    total = usage.get("total_tokens") if isinstance(usage, Mapping) else None
    return choices == [] and total == 0


def judge_chat(status: int, body: object) -> ProbeOutcome:
    """PASS on a 200 with PONG (or non-empty content not cut by length) and no fake signs."""
    if status == 200 and is_fake_200(body):
        return ProbeOutcome.FAIL_FAKE
    if status != 200 or not isinstance(body, Mapping):
        return ProbeOutcome.FAIL
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return ProbeOutcome.FAIL
    first = choices[0]
    message = first.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        return ProbeOutcome.FAIL
    if "PONG" in content.upper() or first.get("finish_reason") != "length":
        return ProbeOutcome.PASS
    return ProbeOutcome.FAIL
