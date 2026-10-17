"""Probe judging: fake-200 guard, chat/tool/needle judges and probe policy (spec section 2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
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


# --- phase b: tool and needle judges, probe policy, usable-context seeds ---

PROBES_FLAG_ENV = "VERDICT_PIPELINE_PROBES"
BUDGET_FALLBACK_HEADER = {"X-OmniRoute-Budget-Fallback": "strict"}
CONTEXT_STEPS = (128_000, 256_000, 512_000, 825_000, 1_000_000)
TOS_AVOID_PREFIXES = frozenset({"kiro"})  # O1: agy is sanctioned, not ToS-avoid
NEVER_PROBE_PREFIXES = frozenset(
    {"ahm", "cc", "claude", "cx", "opencode", "opencode-zen", "bedrock"}
)


def probes_enabled() -> bool:
    import os

    return os.getenv(PROBES_FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def probe_allowed(route: str, *, free_status: str) -> bool:
    """No paid usage: only FREE routes outside the ToS-avoid and blocklisted prefixes."""
    prefix = route.split("/", 1)[0].lower()
    if prefix in TOS_AVOID_PREFIXES or prefix in NEVER_PROBE_PREFIXES:
        return False
    return free_status == "free"


def _first_choice(body: object) -> Mapping[str, object] | None:
    if not isinstance(body, Mapping):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    return choices[0]


def judge_tool(status: int, body: object) -> ProbeOutcome:
    """PASS only on finish_reason=tool_calls with arguments that parse; text tool calls FAIL."""
    import json

    if status == 200 and is_fake_200(body):
        return ProbeOutcome.FAIL_FAKE
    first = _first_choice(body) if status == 200 else None
    if first is None or first.get("finish_reason") != "tool_calls":
        return ProbeOutcome.FAIL
    message = first.get("message")
    calls = message.get("tool_calls") if isinstance(message, Mapping) else None
    if not isinstance(calls, list) or not calls:
        return ProbeOutcome.FAIL
    for call in calls:
        fn = call.get("function") if isinstance(call, Mapping) else None
        args = fn.get("arguments") if isinstance(fn, Mapping) else None
        if not isinstance(args, str):
            return ProbeOutcome.FAIL
        try:
            json.loads(args or "{}")
        except ValueError:
            return ProbeOutcome.FAIL
    return ProbeOutcome.PASS


def needle_payload(route: str, size_tokens: int, needle: str) -> dict[str, object]:
    """Filler of about ``size_tokens`` with the needle at 50% depth (4 chars per token)."""
    half = "lorem ipsum " * max(1, (size_tokens * 4) // 24)
    text = f"{half}\nThe secret code is {needle}.\n{half}"
    return {
        "model": route,
        "stream": False,
        "max_tokens": 32,
        "messages": [
            {"role": "user", "content": text + "\nWhat is the secret code? Reply with it only."}
        ],
    }


def judge_needle(status: int, body: object, needle: str) -> ProbeOutcome:
    if status == 200 and is_fake_200(body):
        return ProbeOutcome.FAIL_FAKE
    first = _first_choice(body) if status == 200 else None
    message = first.get("message") if first is not None else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str) and needle in content:
        return ProbeOutcome.PASS
    return ProbeOutcome.FAIL


def next_context_step(current: int | None, catalog_max: int | None) -> int | None:
    """The next size to try above ``current``, capped at the catalog value."""
    for step in CONTEXT_STEPS:
        if catalog_max is not None and step > catalog_max:
            return None
        if current is None or step > current:
            return step
    return None


@dataclass(frozen=True)
class ContextSeed:
    usable_context: int
    measured_at: datetime | None
    source: str


_SEED_AT = datetime(2026, 10, 17, 4, 10, tzinfo=timezone.utc)


def seed_usable_context() -> dict[str, ContextSeed]:
    """Existing needle evidence (spec 0.1) so these are not re-probed at once."""
    return {
        "nvidia/z-ai/glm-5.3": ContextSeed(825_000, _SEED_AT, "ms-card 04:10Z"),
        "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b": ContextSeed(
            825_000, _SEED_AT, "ms-card 04:10Z"
        ),
        "kc/nvidia/nemotron-3.5-super-vl:free": ContextSeed(700_000, _SEED_AT, "ms-card"),
        "kc/reflection/beam-free": ContextSeed(570_000, _SEED_AT, "ms-card"),
    }
