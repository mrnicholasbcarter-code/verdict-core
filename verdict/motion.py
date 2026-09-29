"""Pure, event-driven motion helpers. No scheduler, input polling, or delays.

A clock measures time, not progress. Only a caller-observed RUNNING state may
pulse or highlight a border. ``trace_step`` accepts observed-active DAG edges,
never all planned edges. Completed work has no motion; unknown is not active.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import TextIO, TypeVar

from verdict.design import PresentationMode, presentation_mode, state_style

PERIOD_SECONDS = 4.0
SYNC_START = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
_Edge = TypeVar("_Edge")


@dataclass(frozen=True)
class MotionClock:
    """Monotonic phase [0, 1); never schedules work or waits for a frame.

    Inject ``now`` for deterministic tests/replay. Four seconds per cycle keeps
    motion slow. Callers render on their existing nonblocking event/input loop;
    dropped frames are fine. A disabled clock does not even sample time.
    """

    mode: PresentationMode = field(default_factory=presentation_mode)
    period: float = PERIOD_SECONDS
    now: Callable[[], float] = field(default=monotonic, repr=False, compare=False)
    _start: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.period) or self.period <= 0:
            raise ValueError("period must be positive and finite")
        start = self.now() if self.mode.animate else 0.0
        if not math.isfinite(start):
            raise ValueError("clock must return finite monotonic time")
        object.__setattr__(self, "_start", start)

    def phase(self, now: float | None = None) -> float:
        if not self.mode.animate:
            return 0.0
        current = self.now() if now is None else now
        if not math.isfinite(current):
            raise ValueError("clock must return finite monotonic time")
        return (max(0.0, current - self._start) % self.period) / self.period


def _phase(phase: float) -> float:
    if not math.isfinite(phase):
        raise ValueError("phase must be finite")
    return phase % 1.0


def _enabled(mode: PresentationMode | None) -> bool:
    return (presentation_mode() if mode is None else mode).animate


def pulse(phase: float, *, mode: PresentationMode | None = None, state: str = "unknown") -> float:
    """Subtle brightness multiplier [0.85, 1.0]; static full light otherwise.

    Explicit ``state`` prevents decorative or unknown work looking active. Do
    not dim essential text; this multiplier is for an activity light only.
    """
    if not _enabled(mode) or not state_style(state).active:
        return 1.0
    return 0.925 + 0.075 * math.cos(math.tau * _phase(phase))


def border_highlight(
    phase: float, length: int, *, mode: PresentationMode | None = None, state: str = "unknown"
) -> int | None:
    """One perimeter cell to recolour, not replace; None leaves a static border.

    Measure length again after resize. This never drops structural border cells.
    """
    if length <= 0 or not _enabled(mode) or not state_style(state).active:
        return None
    return min(length - 1, int(_phase(phase) * length))


def trace_step(
    phase: float, edges: Sequence[_Edge], *, mode: PresentationMode | None = None
) -> _Edge | None:
    """Highlight one observed-active edge; empty/disabled means no trace.

    The caller must filter edges using real RUNNING events, not planned graph
    topology. This is a visual position, never progress or evidence of execution.
    """
    if not edges or not _enabled(mode):
        return None
    return edges[min(len(edges) - 1, int(_phase(phase) * len(edges)))]


def supports_synchronized_output(env: dict[str, str] | None = None) -> bool:
    """Conservative DEC 2026 hint; a caller may instead use a terminal probe.

    Do not send a blocking query, or assume tmux forwards the extension. Unknown
    terminals use ordinary writes. No escape bytes are sent by this probe.
    """
    env = dict(os.environ) if env is None else env
    if env.get("TMUX"):
        return False
    return env.get("TERM", "").lower() in {"xterm-kitty", "foot", "foot-extra"} or env.get(
        "TERM_PROGRAM", ""
    ).lower() in {"wezterm", "ghostty", "iterm.app"}


@contextmanager
def synchronized_output(
    stream: TextIO,
    *,
    mode: PresentationMode | None = None,
    supported: bool | None = None,
    env: dict[str, str] | None = None,
) -> Iterator[None]:
    """Group one short frame with DEC 2026; always release even on failure.

    No cursor hiding or alternate screen ownership here. Consumers must restore
    any terminal state they change and must not wait for input inside this block.
    Plain and reduced-motion output never emits these control sequences.
    """
    mode = presentation_mode(stream, env) if mode is None else mode
    enabled = (
        mode.color
        and mode.animate
        and (supports_synchronized_output(env) if supported is None else supported)
    )
    try:
        if enabled:
            stream.write(SYNC_START)
        yield
    finally:
        if enabled:
            stream.write(SYNC_END)
            stream.flush()
