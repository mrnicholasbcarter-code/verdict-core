"""Candidate-budgeted, source-attributed, injection-safe ContextPack compiler.

Compiles candidate MemoryRecords, receipts, prompt slots, and dynamic context
into a token-budgeted, injection-safe ContextPack for qualified AI models.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from time import time
from typing import Any, Literal

SlotType = Literal["system", "receipt", "memory", "dynamic"]


@dataclass(frozen=True)
class ContextPackSlot:
    """A single source-attributed prompt slot or memory candidate."""

    slot_type: SlotType
    key: str
    content: str
    source: str
    confidence: float = 1.0
    sensitivity: str = "public"
    created_at: float = field(default_factory=time)


@dataclass(frozen=True)
class ContextPack:
    """Compiled, budgeted, and injection-safe context payload."""

    pack_id: str
    compiled_prompt: str
    used_tokens: int
    token_budget: int
    slots: tuple[ContextPackSlot, ...]
    conflicts: tuple[dict[str, Any], ...]
    truncated_count: int
    created_at: float


def sanitize_injection_patterns(text: str) -> str:
    """Sanitize control structures and prompt injection attempts in raw text."""
    patterns = [
        (r"<system>", "&lt;system&gt;"),
        (r"</system>", "&lt;/system&gt;"),
        (r"\[INST\]", "\\[INST\\]"),
        (r"\[/INST\]", "\\[/INST\\]"),
        (r"(?i)\bSystem:\s*", "System (quoted): "),
        (r"(?i)\bUser:\s*", "User (quoted): "),
        (r"(?i)\bAssistant:\s*", "Assistant (quoted): "),
    ]
    sanitized = text
    for pattern, replacement in patterns:
        sanitized = re.sub(pattern, replacement, sanitized)
    return sanitized


def estimate_tokens(text: str) -> int:
    """Rough conservative offline token count estimator (4 chars per token)."""
    return max(1, (len(text) + 3) // 4)


class ContextPackCompiler:
    """Offline, deterministic compiler for ContextPack generation."""

    def __init__(self, default_token_budget: int = 4096) -> None:
        self.default_token_budget = default_token_budget

    def compile(
        self,
        slots: list[ContextPackSlot],
        token_budget: int | None = None,
        pack_id: str | None = None,
    ) -> ContextPack:
        """Compile slots into a budgeted, sanitized ContextPack."""
        budget = token_budget or self.default_token_budget
        if budget <= 0:
            raise ValueError("token_budget_must_be_positive")

        precedence_map: dict[SlotType, int] = {"system": 0, "receipt": 1, "memory": 2, "dynamic": 3}

        sorted_slots = sorted(
            slots,
            key=lambda s: (
                precedence_map.get(s.slot_type, 99),
                -s.confidence,
                -s.created_at,
                s.key,
            ),
        )

        conflicts: list[dict[str, Any]] = []
        seen_keys: dict[str, str] = {}
        for slot in sorted_slots:
            if slot.key in seen_keys and seen_keys[slot.key] != slot.content:
                conflicts.append(
                    {
                        "key": slot.key,
                        "slot_type": slot.slot_type,
                        "source": slot.source,
                        "existing_content_hash": hashlib.sha256(
                            seen_keys[slot.key].encode()
                        ).hexdigest()[:16],
                        "new_content_hash": hashlib.sha256(slot.content.encode()).hexdigest()[:16],
                        "reason": "duplicate_key_contradictory_content",
                    }
                )
            else:
                seen_keys[slot.key] = slot.content

        compiled_parts: list[str] = []
        included_slots: list[ContextPackSlot] = []
        current_tokens = 0
        truncated_count = 0

        for slot in sorted_slots:
            sanitized_content = sanitize_injection_patterns(slot.content)
            header = f"[{slot.slot_type.upper()}:{slot.key} (source: {slot.source})]"
            part_str = f"{header}\n{sanitized_content}\n"
            part_tokens = estimate_tokens(part_str)

            if current_tokens + part_tokens <= budget:
                compiled_parts.append(part_str)
                included_slots.append(slot)
                current_tokens += part_tokens
            else:
                truncated_count += 1

        compiled_prompt = "\n".join(compiled_parts)
        actual_id = pack_id or hashlib.sha256(compiled_prompt.encode()).hexdigest()[:16]

        return ContextPack(
            pack_id=actual_id,
            compiled_prompt=compiled_prompt,
            used_tokens=current_tokens,
            token_budget=budget,
            slots=tuple(included_slots),
            conflicts=tuple(conflicts),
            truncated_count=truncated_count,
            created_at=time(),
        )


__all__ = [
    "ContextPack",
    "ContextPackCompiler",
    "ContextPackSlot",
    "SlotType",
    "estimate_tokens",
    "sanitize_injection_patterns",
]
