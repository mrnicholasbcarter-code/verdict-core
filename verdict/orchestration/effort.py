"""Choose only documented Prime thinking levels, bounded by model support."""

from collections.abc import Mapping
from typing import Any

PRIME_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_TASK_EFFORT = {"implement": "medium", "review": "high", "plan": "high", "trivial": "low"}
_NODE_TASK_KIND = {
    "implement": "implement",
    "review": "review",
    "research": "plan",
    "integrate": "implement",
}


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


def task_kind_for_node(kind: object) -> str:
    """Map a DAG node kind to the ``choose_effort`` task kind.

    ``NodeKind.RESEARCH`` is planning work (high). ``NodeKind.INTEGRATE`` is
    implementation work (medium). Unknown kinds are refused, not guessed.
    """
    raw = getattr(kind, "value", kind)
    value = raw if isinstance(raw, str) else str(raw)
    try:
        return _NODE_TASK_KIND[value]
    except KeyError as exc:
        raise ValueError(f"no effort task kind for node kind {value!r}") from exc


def allowlisted_effort_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the model facts effort policy reads. Never price, secrets, or raw inventory."""
    facts: dict[str, Any] = {}
    if "reasoning" in row:
        facts["reasoning"] = row["reasoning"]
    levels = row.get("thinkingLevelMap")
    if isinstance(levels, Mapping):
        facts["thinkingLevelMap"] = {
            level: levels[level] for level in PRIME_THINKING_LEVELS if level in levels
        }
    return facts
