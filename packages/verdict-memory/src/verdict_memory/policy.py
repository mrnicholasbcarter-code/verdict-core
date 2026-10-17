"""Deterministic capture policy. No model or network operation."""

from __future__ import annotations

import re

from verdict_memory.contracts import BARE_API_TOKEN_RE, CaptureDecision, LifecycleEvent

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    BARE_API_TOKEN_RE,  # BOD-423: same bare-token shapes the redactor uses
    re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@"),
)
_ALLOWED_SENSITIVITY = frozenset({"standard"})
_NOISE_KINDS = frozenset({"tool", "tools", "tool-output", "tool_output", "runtime-noise"})
_NOISE_EVENT_KINDS = frozenset({"after_tool"})


class MemoryCapturePolicy:
    """Conservative policy reused from Verdict memory_capture_policy semantics."""

    def evaluate(self, event: LifecycleEvent) -> CaptureDecision:
        digest = event.digest()
        if event.sensitivity.casefold() not in _ALLOWED_SENSITIVITY:
            return CaptureDecision(False, "sensitivity_denied", digest)
        if any(pattern.search(event.content) for pattern in _SECRET_PATTERNS):
            return CaptureDecision(False, "secret_detected", digest)
        meta = event.metadata or {}
        if meta.get("mirror_capture") is False:
            return CaptureDecision(False, "capture_disabled", digest)
        kind = event.memory_kind.casefold()
        if (kind in _NOISE_KINDS or event.kind in _NOISE_EVENT_KINDS) and meta.get(
            "mirror_capture"
        ) is not True:
            return CaptureDecision(False, "low_value_tool_noise", digest)
        return CaptureDecision(True, "eligible", digest)


__all__ = ["MemoryCapturePolicy"]
