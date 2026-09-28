"""Verdict design system — single source for tokens, glyphs, and presentation policy."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Semantic colour tokens
# All keys that existed in terminal_ui.TOKENS are preserved unchanged.
# New keys: DEGRADED, COOLDOWN, DISABLED, UNKNOWN.
# WARNING and ACCENT are distinct semantics (both share #e5b567 today, but
# carry different meanings so consumers can evolve them independently).
# ---------------------------------------------------------------------------

TOKENS: dict[str, str] = {
    # --- existing keys (values unchanged) ---
    "PRIMARY": "bold #54c7b0",
    "SECONDARY": "#8db8d8",
    "ACCENT": "#e5b567",
    "MUTED": "#929ca6",
    "SUCCESS": "#83c995",
    "WARNING": "#e5b567",
    "ERROR": "bold #ee8585",
    "INFO": "#8db8d8",
    "BORDER": "#647580",
    # --- new semantic keys ---
    "DEGRADED": "#c9956a",  # amber-brown: reduced capability, not an error
    "COOLDOWN": "#8db8d8",  # blue: temporary pause / rate-limited
    "DISABLED": "#647580",  # muted grey: intentionally inactive
    "UNKNOWN": "#929ca6",  # same hue as MUTED: state not yet determined
}

# ---------------------------------------------------------------------------
# State glyphs
# Moved from verdict/orchestration/tui.py:48-65.
# Each entry: (rich_glyph, ascii_glyph, token_key)
# ---------------------------------------------------------------------------

GLYPHS: dict[str, tuple[str, str, str]] = {
    "running": ("\u25cf", "*", "PRIMARY"),
    "validated": ("\u2713", "+", "SUCCESS"),
    "failed": ("\u2717", "x", "ERROR"),
    "reassigned": ("\u21bb", "~", "WARNING"),
    "planned": ("\u25cc", "o", "MUTED"),
    "blocked": ("\u25a0", "#", "ERROR"),
}


# ---------------------------------------------------------------------------
# Presentation mode
# ---------------------------------------------------------------------------


@runtime_checkable
class _HasIsatty(Protocol):
    def isatty(self) -> bool: ...


@runtime_checkable
class _HasWidth(Protocol):
    @property
    def width(self) -> int: ...


@dataclass(frozen=True)
class PresentationMode:
    """Resolved presentation policy for one output stream."""

    color: bool
    unicode: bool
    animate: bool
    width: int


def presentation_mode(
    stream: object | None = None, env: dict[str, str] | None = None
) -> PresentationMode:
    """Decide presentation policy from stream capabilities and environment.

    Parameters
    ----------
    stream:
        A file-like object whose ``isatty()`` method is consulted.  If
        ``None``, ``sys.stdout`` is used.
    env:
        Override mapping for environment variables.  ``None`` reads
        ``os.environ`` directly (the normal case).

    Policy rules (evaluated in priority order):
    - Non-TTY, NO_COLOR, CI, TERM=dumb -> color=False, unicode=False,
      animate=False.
    - VERDICT_NO_ANIMATION=1 or reduced-motion hint -> animate=False.
    - SSH_CONNECTION present -> animate allowed but capped (no busy-wait);
      the flag is reflected in the returned mode as animate=True.
    - TMUX present -> color=True, unicode=True, animate=True (tmux forwards
      truecolor when configured; we trust the outer TTY check already fired).
    - COLUMNS env var overrides terminal width detection.

    Contract: this function MUST NOT call time.sleep() or any delay
    primitive.  Animation policy is a flag; callers own frame timing.
    """
    import sys  # local import to avoid top-level cost

    _env: dict[str, str] = dict(os.environ) if env is None else env

    # --- TTY detection ---
    _stream: object = sys.stdout if stream is None else stream

    try:
        is_tty: bool = isinstance(_stream, _HasIsatty) and bool(_stream.isatty())
    except Exception:
        is_tty = False

    # --- forced-plain conditions ---
    no_color = "NO_COLOR" in _env
    ci = bool(_env.get("CI", ""))
    dumb = _env.get("TERM", "") == "dumb"
    forced_plain = _env.get("VERDICT_PLAIN", "") == "1"
    plain = not is_tty or no_color or ci or dumb or forced_plain

    if plain:
        width = _resolve_width(_env, _stream, fallback=80)
        return PresentationMode(color=False, unicode=False, animate=False, width=width)

    # --- animation policy ---
    no_anim = _env.get("VERDICT_NO_ANIMATION", "") == "1"
    animate = not no_anim

    # --- width ---
    width = _resolve_width(_env, _stream, fallback=80)

    return PresentationMode(color=True, unicode=True, animate=animate, width=width)


def _resolve_width(env: dict[str, str], stream: object, fallback: int = 80) -> int:
    """Return terminal width: COLUMNS env -> stream.width -> shutil.get_terminal_size -> fallback."""
    col_env = env.get("COLUMNS", "")
    if col_env.isdigit():
        return max(1, int(col_env))
    if isinstance(stream, _HasWidth):
        w = stream.width
        if w > 0:
            return w
    try:
        return shutil.get_terminal_size(fallback=(fallback, 24)).columns
    except Exception:
        return fallback
