"""Shared action layer — the ONLY place CLI and TUI meet domain services."""

from verdict.actions.base import ActionEvent, ActionResult, ActionSink, ActionSpec
from verdict.actions.registry import MACHINE_ONLY, get_action, list_actions, run_action

__all__ = [
    "MACHINE_ONLY",
    "ActionEvent",
    "ActionResult",
    "ActionSink",
    "ActionSpec",
    "get_action",
    "list_actions",
    "run_action",
]
