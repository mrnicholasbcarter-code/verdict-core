"""Deterministic capture policy for optional shared-memory mirroring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from verdict.memory_plane import MemoryRecord

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)https?://[^\s/@]+:[^\s/@]+@"),
)
_ALLOWED_SENSITIVITY = frozenset({"standard"})
_NOISE_NAMESPACES = frozenset({"tool", "tools", "tool-output", "tool_output", "runtime-noise"})
_NOISE_SOURCES = frozenset({"tool", "tool-output", "tool_result", "runtime"})


@dataclass(frozen=True)
class CaptureDecision:
    """Mechanical capture result. Reasons are stable audit labels."""

    eligible: bool
    reason: str


class MemoryCapturePolicy:
    """Conservative policy. It performs no model or network operation."""

    def evaluate(self, record: MemoryRecord) -> CaptureDecision:
        if record.status != "active":
            return CaptureDecision(False, "record_not_active")
        if record.sensitivity.casefold() not in _ALLOWED_SENSITIVITY:
            return CaptureDecision(False, "sensitivity_denied")
        if any(pattern.search(record.content) for pattern in _SECRET_PATTERNS):
            return CaptureDecision(False, "secret_detected")
        namespace = record.namespace.casefold()
        source = record.source.casefold()
        metadata = record.metadata or {}
        if (namespace in _NOISE_NAMESPACES or source in _NOISE_SOURCES) and metadata.get(
            "mirror_capture"
        ) is not True:
            return CaptureDecision(False, "low_value_tool_noise")
        if metadata.get("mirror_capture") is False:
            return CaptureDecision(False, "capture_disabled")
        return CaptureDecision(True, "eligible")


__all__ = ["CaptureDecision", "MemoryCapturePolicy"]
