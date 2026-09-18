"""Worthiness classifier: worthy vs ordinary with named reasons (BOD-107).

Worthiness runs before cheapness. Rules are explicit keyword/escalation matches —
never an invented score. ``worthy`` admits frontier/high-cap models only;
``ordinary`` is free-first then lesser-paid (BOD-109).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from verdict.escalation import scan

TaskClass = Literal["worthy", "ordinary"]

WORTHY: TaskClass = "worthy"
ORDINARY: TaskClass = "ordinary"

_WORTHY_PHRASES = (
    "system design",
    "distributed",
    "infrastructure",
    "architect",
    "architecture",
    "threat model",
    "security audit",
    "vulnerability",
    "final review",
    "orchestration",
    "hard debug",
    "hard-debug",
    "production deploy",
    "credential",
    "secret rotation",
    "security",
)

_ORDINARY_TOKENS = (
    "summarize",
    "rewrite",
    "format",
    "typo",
    "comment",
    "rename",
    "lint",
    "docstring",
)

_HIGH_CRITICALITY = frozenset({"critical", "high"})


@dataclass(frozen=True)
class WorthinessClassification:
    """Receipt contract: task_class + named class_reasons."""

    task_class: TaskClass
    class_reasons: tuple[str, ...]

    @property
    def protected_ranker_class(self) -> str:
        """Chooser task_class: protected ranker for worthy, ordinary otherwise."""
        return "architecture" if self.task_class == WORTHY else "implementation"

    def to_dict(self) -> dict[str, Any]:
        return {"task_class": self.task_class, "class_reasons": list(self.class_reasons)}


def _combined_text(task: str, context: Mapping[str, Any] | None) -> str:
    parts = [task]
    if isinstance(context, Mapping):
        for key in ("objective", "task", "task_class"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return " ".join(parts).lower()


def classify_worthiness(
    task: str, *, criticality: str = "medium", context: Mapping[str, Any] | None = None
) -> WorthinessClassification:
    """Classify ``worthy`` vs ``ordinary`` with named reasons (no invented scores)."""
    text = _combined_text(task, context)
    explicit = None if context is None else context.get("task_class")
    if isinstance(explicit, str):
        chosen = explicit.strip().lower()
        if chosen == WORTHY:
            return WorthinessClassification(
                task_class=WORTHY, class_reasons=("explicit task_class=worthy",)
            )
        if chosen == ORDINARY:
            return WorthinessClassification(
                task_class=ORDINARY, class_reasons=("explicit task_class=ordinary",)
            )

    reasons: list[str] = []
    tier, label = scan(task)
    if label in {"architecture", "security", "auth-security", "money-path", "live-execution"}:
        reasons.append(f"escalation:{label}")
    if tier == 0:
        reasons.append("escalation:never-offload-critical")

    lowered_crit = (criticality or "medium").strip().lower()
    for phrase in _WORTHY_PHRASES:
        if phrase in text:
            reasons.append(f"phrase:{phrase}")

    if lowered_crit in _HIGH_CRITICALITY and reasons:
        reasons.append(f"criticality:{lowered_crit}")

    if reasons:
        return WorthinessClassification(
            task_class=WORTHY, class_reasons=tuple(dict.fromkeys(reasons))
        )

    ordinary_hits = [token for token in _ORDINARY_TOKENS if token in text]
    ordinary_reasons = (
        tuple(f"ordinary:{hit}" for hit in ordinary_hits)
        if ordinary_hits
        else ("no worthy markers; default ordinary coding",)
    )
    return WorthinessClassification(task_class=ORDINARY, class_reasons=ordinary_reasons)
