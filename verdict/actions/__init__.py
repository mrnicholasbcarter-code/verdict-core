"""Verdict action layer — shared CLI + TUI convergence surface."""

from verdict.actions.base import ActionEvent, ActionResult, ActionSink, ActionSpec
from verdict.actions.registry import (
    GAP,
    LAUNCH,
    MACHINE_ONLY,
    get_action,
    list_actions,
    register,
    run_action,
)

__all__ = [
    "GAP",
    "LAUNCH",
    "MACHINE_ONLY",
    "ActionEvent",
    "ActionResult",
    "ActionSink",
    "ActionSpec",
    "get_action",
    "list_actions",
    "register",
    "run_action",
]
