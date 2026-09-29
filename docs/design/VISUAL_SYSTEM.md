# Verdict visual system

`verdict.design` owns palette, semantic aliases, state labels, spacing, and panels.
`verdict.motion` owns pure motion math and optional synchronized frame writes.
Consumers supply execution facts; neither module runs work or invents progress.
Verdict keeps its name and wordmark. No Prime artwork is used.

## Tokens

RGB is authoritative. `TOKENS` contains Rich truecolor styles. `tokens_for()` and
`token_style()` accept `truecolor`, `256`, `standard` (ANSI 16), or `None` (plain).
`color_capability(stream, env)` uses `COLORTERM` and `TERM`, after plain policy.
Rich also downgrades truecolor styles for consoles that negotiate fewer colours.

| Token | RGB / hex | xterm-256 | ANSI-16 |
| --- | --- | --- | --- |
| BACKGROUND | 16,16,20 / `#101014` | 233 | black |
| SURFACE | 24,24,27 / `#18181b` | 234 | black |
| TEXT | 244,244,245 / `#f4f4f5` | 255 | bright_white |
| SECONDARY | 161,161,170 / `#a1a1aa` | 248 | white |
| MUTED | 146,146,158 / `#92929e` | 246 | white |
| PURPLE | 167,139,250 / `#a78bfa` | 147 | bright_magenta |
| CYAN | 34,184,235 / `#22b8eb` | 39 | bright_cyan |
| SUCCESS | 74,222,128 / `#4ade80` | 84 | bright_green |
| AMBER | 245,176,11 / `#f5b00b` | 214 | bright_yellow |
| RED | 248,113,113 / `#f87171` | 210 | bright_red |
| BORDER | 82,82,91 / `#52525b` | 240 | bright_black |

Legacy names remain: PRIMARY → bold PURPLE; ACCENT/INFO → CYAN;
WARNING/DEGRADED/COOLDOWN → AMBER; ERROR → bold RED; DISABLED/UNKNOWN → MUTED.
Legacy GLYPHS entries remain unchanged. Bright text has 16.1:1 contrast on SURFACE;
accents have at least 6.4:1. WCAG tests require 7:1 text and 4.5:1 accents on both
surfaces. Reference purple/cyan were brightened for contrast. ANSI palettes are
user-configurable, so colour is never the only state signal.

## States

`state_style()` accepts lower-case worker strings and `NodeState` values.
`render_state()` always includes a glyph and explicit label, with ASCII fallback.

| Observed state | Label / colour | Motion |
| --- | --- | --- |
| planned | ◌ PLANNED / muted | none |
| admitted | ○ ADMITTED / cyan | none; admission is not execution |
| dispatched | → DISPATCHED / cyan | none; dispatch is not observed running |
| running | ● RUNNING / purple | subtle light pulse; active border |
| cooldown | ◷ COOLDOWN (remaining) / amber | none; caller supplies real timer |
| failed / terminal_failure | ✗ FAILED / red | none; remains visible |
| rejected / blocked | ✗ REJECTED / ■ BLOCKED / red | none; remains visible |
| terminal_success | ○ AWAITING VALIDATION / cyan | none; not validated yet |
| validated | ✓ VALIDATED / green | none; settled success |
| reassigned | ↻ REASSIGNED / amber | none |
| unknown | ? UNKNOWN / muted | none; never guessed active |

Unknown cooldown time is `? remaining`, never an invented countdown. A timer
reaching zero does not imply the provider is available. Consumers update state
only from observed events. Keep failures visible even when later attempts run.

## Panels and hierarchy

Use `panel()` / `panel_box()`: rounded box, left-aligned title, `(1, 2)` vertical /
horizontal padding, bright text on charcoal. Spacing is 0 / 1 / 2 / 4 cells.
Strings and titles are literal text, not markup. Plain panels use ASCII borders.
Rich measures width on each render; reuse the panel after resize. Borders remain
complete at 60 and 200 columns. Explicit width is capped to the current console.
Existing screens opt in; their plain/NO_COLOR output stays byte-identical.

Keep goal, active work, selected models, progress and controls visible together.
Prefer a stable layout over moving text. Decorative art must not resemble an
execution indicator. These primitives do not add decorative motion.

## Motion and terminal rules

- `MotionClock.phase()` uses monotonic time, four seconds per cycle by default.
  It never sleeps or schedules a refresh. Animation never delays work or input.
- Consumers use their existing nonblocking input/event loop. Refresh at most
  roughly 5–6 times per second (the reference preview uses 0.18 s); drop frames
  when busy, never block for an animation. Do not wait for input inside a frame.
- `pulse()` only changes a small activity light (0.85–1.0), never essential text.
  `border_highlight()` returns one cell index to recolour, not remove. Both
  require an explicit observed RUNNING state. Other states remain static.
- `trace_step()` accepts **only observed-active DAG edges**, not the planned
  graph. An empty set or disabled motion returns no highlight. Traces are not
  progress measurements or proof of execution.
- All motion is static for non-TTY, NO_COLOR, VERDICT_PLAIN=1, CI, TERM=dumb,
  VERDICT_NO_ANIMATION=1, VERDICT_REDUCED_MOTION=1, or REDUCED_MOTION=1.
  Motion-only flags also accept `true`, `yes`, `on`, `reduce` (case-insensitive).
- `synchronized_output()` wraps one short frame in DEC 2026 when supported.
  Known Kitty/Foot/WezTerm/Ghostty/iTerm hints enable it; unknown terminals and
  tmux default off. Callers may pass a negotiated capability. The final marker
  is written even on a render exception. Plain/reduced-motion emit no markers.
- Consumers own cursor/alternate-screen restoration in `finally`. These helpers
  do not hide the cursor, read keys, or change terminal modes.

## Example

```python
from rich.console import Console
from verdict.design import panel, presentation_mode, render_state
from verdict.motion import MotionClock, border_highlight, pulse, trace_step

console = Console()
mode = presentation_mode(console.file)
clock = MotionClock(mode)
# On an existing event/input tick; observed_state and active_edges come from events:
phase = clock.phase()
light = pulse(phase, mode=mode, state=observed_state)
cell = border_highlight(phase, perimeter_length, mode=mode, state=observed_state)
edge = trace_step(phase, active_edges, mode=mode)
console.print(panel(render_state(observed_state, mode=mode), title="Active work", mode=mode))
```

The test suite also compares SHA-256 byte captures of setup, home and orchestration
plain renders to origin/main `4073a1840219cc2870bd363938d87e4fd2a6aba4` at widths
60, 100 and 200. It never enters an interactive UI loop.
