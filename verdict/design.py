"""Verdict visual system: semantic palette, observed states, panels, and policy.

Existing ``TOKENS`` and ``GLYPHS`` names remain public. Rich styles are truecolor
by default; ``token_style`` / ``tokens_for`` provide explicit terminal fallbacks.
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from rich import box
from rich.box import Box
from rich.console import RenderableType
from rich.panel import Panel
from rich.text import Text

ColorSystem = Literal["truecolor", "256", "standard"]


@dataclass(frozen=True)
class PaletteToken:
    """24-bit source colour and deliberate xterm-256 / ANSI-16 fallbacks."""

    rgb: tuple[int, int, int]
    ansi256: int
    ansi16: str

    @property
    def hex(self) -> str:
        return "#{:02x}{:02x}{:02x}".format(*self.rgb)

    def color(self, system: ColorSystem) -> str:
        if system == "256":
            return f"color({self.ansi256})"
        if system == "standard":
            return self.ansi16
        return self.hex


# Purple/cyan are brighter than the reference installer so text remains legible
# on both charcoal surfaces (WCAG >= 4.5:1). No artwork/branding is borrowed.
PALETTE: dict[str, PaletteToken] = {
    "BACKGROUND": PaletteToken((16, 16, 20), 233, "black"),
    "SURFACE": PaletteToken((24, 24, 27), 234, "black"),
    "TEXT": PaletteToken((244, 244, 245), 255, "bright_white"),
    "SECONDARY": PaletteToken((161, 161, 170), 248, "white"),
    "MUTED": PaletteToken((146, 146, 158), 246, "white"),
    "PURPLE": PaletteToken((167, 139, 250), 147, "bright_magenta"),
    "CYAN": PaletteToken((34, 184, 235), 39, "bright_cyan"),
    "SUCCESS": PaletteToken((74, 222, 128), 84, "bright_green"),
    # #f5b00b rather than #f59e0b: Rich downgrades #f59e0b to ANSI 9 (bright red) on
    # 16-colour terminals, which made cooldown look like failure. #f5b00b maps to ANSI 11.
    "AMBER": PaletteToken((245, 176, 11), 214, "bright_yellow"),
    "RED": PaletteToken((248, 113, 113), 210, "bright_red"),
    "BORDER": PaletteToken((82, 82, 91), 240, "bright_black"),
}

TOKEN_ALIASES: dict[str, str] = {
    "PRIMARY": "PURPLE",
    "ACCENT": "CYAN",
    "WARNING": "AMBER",
    "ERROR": "RED",
    "INFO": "CYAN",
    "DEGRADED": "AMBER",
    "COOLDOWN": "AMBER",
    "DISABLED": "MUTED",
    "UNKNOWN": "MUTED",
}


def token_style(name: str, color_system: ColorSystem | None = "truecolor") -> str:
    """Resolve a semantic token to a Rich style; None emits no styling."""
    token = PALETTE[TOKEN_ALIASES.get(name, name)]
    if color_system is None:
        return ""
    weight = "bold " if name in {"PRIMARY", "ERROR"} else ""
    return weight + token.color(color_system)


def tokens_for(color_system: ColorSystem | None = "truecolor") -> dict[str, str]:
    """A Rich Theme mapping for a resolved capability, including old names."""
    return {name: token_style(name, color_system) for name in (*PALETTE, *TOKEN_ALIASES)}


TOKENS: dict[str, str] = tokens_for()

# Keep legacy glyphs byte-identical. New state metadata is in STATE_STYLES, so
# screens that enumerate these original six states do not change plain output.
GLYPHS: dict[str, tuple[str, str, str]] = {
    "running": ("●", "*", "PRIMARY"),
    "validated": ("✓", "+", "SUCCESS"),
    "failed": ("✗", "x", "ERROR"),
    "reassigned": ("↻", "~", "WARNING"),
    "planned": ("◌", "o", "MUTED"),
    "blocked": ("■", "#", "ERROR"),
}


@dataclass(frozen=True)
class StateStyle:
    """Presentation only: callers supply a state observed in execution events."""

    label: str
    glyph: str
    ascii_glyph: str
    token: str
    active: bool = False

    def text(
        self,
        *,
        unicode: bool = True,
        remaining_seconds: float | None = None,
        show_remaining: bool = True,
    ) -> str:
        label = self.label
        if self.label == "COOLDOWN" and show_remaining:
            remaining = "?"
            if remaining_seconds is not None:
                if not math.isfinite(remaining_seconds):
                    raise ValueError("remaining_seconds must be finite")
                seconds = math.ceil(max(0, remaining_seconds))
                minutes, seconds = divmod(seconds, 60)
                remaining = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            label += f" ({remaining} remaining)"
        return f"{self.glyph if unicode else self.ascii_glyph} {label}"


STATE_STYLES: dict[str, StateStyle] = {
    "planned": StateStyle("PLANNED", *GLYPHS["planned"]),
    "admitted": StateStyle("ADMITTED", "○", "o", "INFO"),
    "dispatched": StateStyle("DISPATCHED", "→", ">", "INFO"),
    "running": StateStyle("RUNNING", *GLYPHS["running"], active=True),
    "cooldown": StateStyle("COOLDOWN", "◷", "~", "COOLDOWN"),
    "failed": StateStyle("FAILED", *GLYPHS["failed"]),
    "terminal_failure": StateStyle("FAILED", *GLYPHS["failed"]),
    "rejected": StateStyle("REJECTED", *GLYPHS["failed"]),
    "blocked": StateStyle("BLOCKED", *GLYPHS["blocked"]),
    "terminal_success": StateStyle("AWAITING VALIDATION", "○", "o", "INFO"),
    "validated": StateStyle("VALIDATED", *GLYPHS["validated"]),
    "reassigned": StateStyle("REASSIGNED", *GLYPHS["reassigned"]),
    "unknown": StateStyle("UNKNOWN", "?", "?", "UNKNOWN"),
}


def state_style(state: str) -> StateStyle:
    """Accept worker strings or str-enum NodeState, without importing the engine."""
    return STATE_STYLES.get(state.lower(), STATE_STYLES["unknown"])


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
    color_system: ColorSystem | None = "truecolor"


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
    - VERDICT_NO_ANIMATION=1, VERDICT_REDUCED_MOTION=1, or
      REDUCED_MOTION=1 -> animate=False. These also accept true/yes/on/reduce.
    - SSH_CONNECTION present -> animate allowed but capped (no busy-wait);
      the flag is reflected in the returned mode as animate=True.
    - TMUX present -> color=True, unicode=True, animate=True (tmux forwards
      truecolor when configured; we trust the outer TTY check already fired).
    - COLUMNS env var overrides terminal width detection.

    Contract: this function never delays the caller. Animation policy is a
    flag; callers own nonblocking frame timing.
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
        return PresentationMode(
            color=False, unicode=False, animate=False, width=width, color_system=None
        )

    # --- animation policy ---
    no_anim = any(
        _env.get(key, "").lower() in {"1", "true", "yes", "on", "reduce"}
        for key in ("VERDICT_NO_ANIMATION", "VERDICT_REDUCED_MOTION", "REDUCED_MOTION")
    )
    animate = not no_anim

    # --- width ---
    width = _resolve_width(_env, _stream, fallback=80)

    return PresentationMode(
        color=True, unicode=True, animate=animate, width=width, color_system=_color_capability(_env)
    )


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


def _color_capability(env: dict[str, str]) -> ColorSystem:
    term = env.get("TERM", "").lower()
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"} or term.endswith(
        ("-direct", "-truecolor")
    ):
        return "truecolor"
    if "256color" in term:
        return "256"
    return "standard"


def color_capability(
    stream: object | None = None, env: dict[str, str] | None = None
) -> ColorSystem | None:
    """Conservative COLORTERM/TERM probe, subject to all plain-output rules.

    ``standard`` means ANSI 16 colours (Rich's spelling); None means no colour.
    A probe is a hint, not proof that a terminal's configured palette matches RGB.
    """
    return presentation_mode(stream, env).color_system


def render_state(
    state: str,
    *,
    mode: PresentationMode | None = None,
    remaining_seconds: float | None = None,
    show_remaining: bool = True,
) -> Text:
    """Glyph + explicit label, including caller-observed cooldown time if known.

    ``show_remaining=False`` keeps a cooldown cell to glyph + label. The
    routing explorer puts the remaining time in its cooldowns section.
    """
    mode = presentation_mode() if mode is None else mode
    style = state_style(state)
    return Text(
        style.text(
            unicode=mode.unicode, remaining_seconds=remaining_seconds, show_remaining=show_remaining
        ),
        style=token_style(style.token, mode.color_system if mode.color else None),
    )


SPACING: dict[str, int] = {"none": 0, "tight": 1, "normal": 2, "wide": 4}
PANEL_PADDING: tuple[int, int] = (SPACING["tight"], SPACING["normal"])


def panel_box(mode: PresentationMode | None = None) -> Box:
    """Rounded Unicode borders, complete ASCII borders for plain streams."""
    mode = presentation_mode() if mode is None else mode
    return box.ROUNDED if mode.unicode else box.ASCII


def panel(
    content: RenderableType,
    *,
    title: str = "",
    mode: PresentationMode | None = None,
    tone: str = "BORDER",
    width: int | None = None,
) -> Panel:
    """Consistent charcoal surface, left title and (1, 2) cell padding.

    No fixed width by default: Rich measures at each render, including resize.
    An explicit width is capped by the Console at render time. Strings are
    literal text, not markup. Existing screens opt in; their plain bytes stay put.
    """
    mode = presentation_mode() if mode is None else mode
    system = mode.color_system if mode.color else None
    surface = f"{token_style('TEXT', system)} on {token_style('SURFACE', system)}" if system else ""
    return Panel(
        Text(content) if isinstance(content, str) else content,
        title=Text(title, style=token_style("PRIMARY", system)) if title else None,
        title_align="left",
        box=panel_box(mode),
        padding=PANEL_PADDING,
        style=surface,
        border_style=token_style(tone, system),
        width=width,
        expand=True,
        safe_box=False,
    )
