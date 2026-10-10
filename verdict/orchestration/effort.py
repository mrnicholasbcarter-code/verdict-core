"""Choose only documented Prime thinking levels, bounded by model support."""

from collections.abc import Mapping
from typing import Any

PRIME_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_TASK_EFFORT = {"implement": "medium", "review": "high", "plan": "high", "trivial": "low"}


def choose_effort(task_kind: str, model_row: Mapping[str, Any]) -> str | None:
    """Keep the task default unless support explicitly excludes it; never step up.

    An absent map is unknown: request the task default, subject to Prime validation.
    In a known map, missing/null keys do not declare support. Use only an explicit
    supported default or lower level; otherwise omit the spawn override.
    """
    if model_row.get("reasoning") is False:
        return None
    default = _TASK_EFFORT.get(task_kind, "medium")
    levels = model_row.get("thinkingLevelMap")
    if not isinstance(levels, Mapping):
        return default
    if default in levels and levels[default] is not None:
        return default
    for level in reversed(PRIME_THINKING_LEVELS[: PRIME_THINKING_LEVELS.index(default)]):
        if level in levels and levels[level] is not None:
            return level
    return None


def effort_reason(
    task_kind: str, chosen: str | None, model_row: Mapping[str, Any]
) -> dict[str, Any]:
    """Explain the task policy and any model-support clamp without claiming execution."""
    default = _TASK_EFFORT.get(task_kind, "medium")
    clamp = None
    if model_row.get("reasoning") is False:
        clamp = "reasoning_disabled"
    elif chosen != default:
        clamp = f"{default}->{chosen}" if chosen is not None else "no_supported_level"
    return {"task_kind": task_kind, "default_effort": default, "clamp": clamp}
