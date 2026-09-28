"""Action protocol types — presentation lifecycle only.

ActionEvents are the presentation lifecycle: start/progress/done/error for
CLI spinners and TUI progress.  They must NOT replace or duplicate
orchestration events/receipts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class ActionSpec:
    """Declarative metadata for a registered action."""

    name: str
    family: str
    kind: Literal["read", "mutation"]
    summary: str
    tui_section: str


@dataclass(frozen=True)
class LaunchSpec:
    """Metadata for a long-running/interactive command that launches separately.

    reason: Command-specific explanation of why this launches (not ACTION/MACHINE_ONLY).
    entry: Exact module:function that the CLI handler calls (e.g., 'verdict.cli:cmd_orchestrate').
    section: TUI section grouping for launch actions.
    """
    reason: str
    entry: str
    section: str


@dataclass
class ActionEvent:
    """Presentation lifecycle event (never stored alongside orchestration data)."""

    kind: Literal["start", "progress", "done", "error"]
    action: str
    ts: float = field(default_factory=time.time)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    """Outcome of running an action through the shared layer.

    ``data`` is the JSON-serialisable payload — identical to what ``--json``
    prints today.  ``ok`` and ``exit_code`` let callers decide presentation
    without inspecting the payload.
    """

    data: Any
    ok: bool = True
    exit_code: int = 0
    events: list[ActionEvent] = field(default_factory=list)


@runtime_checkable
class ActionSink(Protocol):
    """Receives presentation events; default is a no-op."""

    def on_event(self, event: ActionEvent) -> None: ...  # pragma: no cover


class _NoOpSink:
    """Default sink — silently discards events."""

    def on_event(self, event: ActionEvent) -> None:
        pass


NOOP_SINK: ActionSink = _NoOpSink()
