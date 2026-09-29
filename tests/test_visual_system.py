"""BOD-280 visual policy, accessibility, state truth, and plain-byte regression."""

from __future__ import annotations

import ast
import hashlib
import io
import math
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

from verdict.design import (
    GLYPHS,
    PALETTE,
    PANEL_PADDING,
    SPACING,
    STATE_STYLES,
    TOKENS,
    ColorSystem,
    PresentationMode,
    color_capability,
    panel,
    panel_box,
    presentation_mode,
    render_state,
    state_style,
    token_style,
    tokens_for,
)
from verdict.home import HomeState, render_home
from verdict.motion import (
    SYNC_END,
    SYNC_START,
    MotionClock,
    border_highlight,
    pulse,
    supports_synchronized_output,
    synchronized_output,
    trace_step,
)
from verdict.orchestration.contracts import NodeState
from verdict.orchestration.tui import render_text
from verdict.terminal_ui import TerminalUI


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


ACTIVE = PresentationMode(True, True, True, 100)


@pytest.mark.parametrize("system", ["truecolor", "256", "standard", None])
def test_tokens_have_explicit_parseable_fallbacks(system: ColorSystem | None) -> None:
    styles = tokens_for(system)
    assert TOKENS.keys() == styles.keys()
    for name, value in styles.items():
        parsed = Style.parse(value)
        assert token_style(name, system) == value
        if system is None:
            assert parsed.color is None and not value
        else:
            assert parsed.color is not None
            assert parsed.color.system.name.lower() == {"256": "eight_bit"}.get(system, system)
    for token in PALETTE.values():
        assert len(token.rgb) == 3 and all(0 <= channel <= 255 for channel in token.rgb)
        assert 0 <= token.ansi256 <= 255


def test_semantic_aliases_and_legacy_glyphs() -> None:
    assert TOKENS["COOLDOWN"] == TOKENS["WARNING"] == TOKENS["AMBER"]
    assert TOKENS["ACCENT"] == TOKENS["CYAN"]
    assert TOKENS["PRIMARY"] == "bold " + TOKENS["PURPLE"]
    assert TOKENS["ERROR"] == "bold " + TOKENS["RED"]
    assert GLYPHS["running"] == ("●", "*", "PRIMARY")
    assert GLYPHS["validated"] == ("✓", "+", "SUCCESS")


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"COLORTERM": "truecolor", "TERM": "xterm-256color"}, "truecolor"),
        ({"COLORTERM": "24bit"}, "truecolor"),
        ({"COLORTERM": "TRUECOLOR"}, "truecolor"),
        ({"TERM": "xterm-direct"}, "truecolor"),
        ({"TERM": "screen-256color"}, "256"),
        ({"TERM": "xterm"}, "standard"),
        ({}, "standard"),
        ({"TERM": "dumb", "COLORTERM": "truecolor"}, None),
        ({"NO_COLOR": "", "COLORTERM": "truecolor"}, None),
    ],
)
def test_capability_probe(env: dict[str, str], expected: str | None) -> None:
    assert color_capability(TTY(), env) == expected
    assert color_capability(io.StringIO(), env) is None


@pytest.mark.parametrize("state", list(NodeState))
def test_all_node_states_have_truthful_styles(state: NodeState) -> None:
    style = state_style(state)
    assert style is not STATE_STYLES["unknown"]
    assert style.token in TOKENS
    assert style.label and style.glyph and style.ascii_glyph.isascii()
    assert style.active == (state is NodeState.RUNNING)
    assert style.label in render_state(state, mode=ACTIVE).plain
    if state is NodeState.TERMINAL_SUCCESS:
        assert style.label == "AWAITING VALIDATION" and style.token != "SUCCESS"


def test_settled_success_and_persistent_failure() -> None:
    assert state_style("VALIDATED").token == "SUCCESS"
    for state in ("failed", "terminal_failure", "blocked", "rejected"):
        assert state_style(state).token == "ERROR"
        for phase in (0, 0.25, 0.5, 0.99):
            assert pulse(phase, mode=ACTIVE, state=state) == 1.0
            assert border_highlight(phase, 80, mode=ACTIVE, state=state) is None
    assert state_style("unrecognised").label == "UNKNOWN"


@pytest.mark.parametrize(
    "remaining,label", [(None, "?"), (0, "0s"), (-10, "0s"), (0.1, "1s"), (61.2, "1m 02s")]
)
def test_cooldown_has_amber_status_and_remaining_time(remaining: float | None, label: str) -> None:
    plain = PresentationMode(False, False, False, 60, None)
    text = render_state("cooldown", mode=plain, remaining_seconds=remaining)
    assert text.plain == f"~ COOLDOWN ({label} remaining)"
    assert not text.style
    assert state_style("cooldown").token == "COOLDOWN"
    assert not state_style("cooldown").active


@pytest.mark.parametrize(
    "env,tty",
    [
        ({"VERDICT_NO_ANIMATION": "1"}, True),
        ({"VERDICT_REDUCED_MOTION": "1"}, True),
        ({"REDUCED_MOTION": "reduce"}, True),
        ({"NO_COLOR": ""}, True),
        ({"VERDICT_PLAIN": "1"}, True),
        ({"CI": "true"}, True),
        ({"TERM": "dumb"}, True),
        ({}, False),
    ],
)
def test_disabled_motion_is_static_and_never_samples_clock(env: dict[str, str], tty: bool) -> None:
    mode = presentation_mode(TTY() if tty else io.StringIO(), env)
    assert not mode.animate

    def forbidden_clock() -> float:
        raise AssertionError("disabled motion sampled clock")

    clock = MotionClock(mode, now=forbidden_clock)
    for phase in (0, 0.2, 0.5, 0.99):
        assert clock.phase(phase) == 0
        assert pulse(phase, mode=mode, state="running") == 1.0
        assert border_highlight(phase, 60, mode=mode, state="running") is None
        assert trace_step(phase, [("A", "B")], mode=mode) is None


def test_clock_phase_determinism_and_nonblocking_sampling() -> None:
    clock = MotionClock(ACTIVE, now=lambda: 10.0)
    assert [clock.phase(now) for now in (10, 11, 12, 13, 14, 9)] == [0, 0.25, 0.5, 0.75, 0, 0]
    assert clock.phase() == 0
    assert MotionClock(ACTIVE, period=8, now=lambda: 0).phase(2) == 0.25


@pytest.mark.parametrize("invalid", [0, -1, math.inf, math.nan])
def test_invalid_period_rejected(invalid: float) -> None:
    with pytest.raises(ValueError):
        MotionClock(ACTIVE, period=invalid)


def test_only_observed_running_states_animate() -> None:
    assert pulse(0, mode=ACTIVE, state="running") == pytest.approx(1)
    assert pulse(0.5, mode=ACTIVE, state="running") == pytest.approx(0.85)
    assert pulse(0.25, mode=ACTIVE, state=NodeState.RUNNING) == pytest.approx(0.925)
    for name, style in STATE_STYLES.items():
        if not style.active:
            assert pulse(0.5, mode=ACTIVE, state=name) == 1
            assert border_highlight(0.5, 60, mode=ACTIVE, state=name) is None
    assert pulse(0.5, mode=ACTIVE) == 1
    assert border_highlight(0.5, 60, mode=ACTIVE) is None


def test_border_and_trace_positions_resize_wrap_and_empty() -> None:
    for length in (60, 200):
        assert border_highlight(0.5, length, mode=ACTIVE, state="running") == length // 2
        assert border_highlight(1, length, mode=ACTIVE, state="running") == 0
    assert border_highlight(0.5, 0, mode=ACTIVE, state="running") is None
    assert trace_step(0.5, [], mode=ACTIVE) is None
    edges = [("A", "B"), ("B", "C")]
    assert trace_step(0, edges, mode=ACTIVE) == edges[0]
    assert trace_step(0.5, edges, mode=ACTIVE) == edges[1]
    assert trace_step(1, edges, mode=ACTIVE) == edges[0]


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_invalid_times_fail_explicitly(invalid: float) -> None:
    with pytest.raises(ValueError):
        MotionClock(ACTIVE).phase(invalid)
    with pytest.raises(ValueError):
        pulse(invalid, mode=ACTIVE, state="running")
    with pytest.raises(ValueError):
        render_state("cooldown", mode=ACTIVE, remaining_seconds=invalid)


@pytest.mark.parametrize("unicode", [True, False])
def test_panel_borders_are_complete_after_resize(unicode: bool) -> None:
    mode = PresentationMode(True, unicode, False, 100)
    component = panel(
        "Goal: verify\nActive: worker\nModel: cc/model\nProgress: 1/2\nControls: q quit",
        title="VERDICT",
        mode=mode,
    )
    assert component.box is panel_box(mode)
    assert component.padding == PANEL_PADDING == (1, 2)
    assert list(SPACING.values()) == [0, 1, 2, 4]
    for width in (60, 200, 60):  # reuse one panel: Rich measures current Console width
        output = io.StringIO()
        Console(file=output, width=width, force_terminal=False, color_system=None).print(component)
        lines = output.getvalue().splitlines()
        assert all(Text(line).cell_len == width for line in lines)
        assert lines[0].startswith("╭" if unicode else "+")
        assert lines[0].endswith("╮" if unicode else "+")
        assert lines[-1].startswith("╰" if unicode else "+")
        assert lines[-1].endswith("╯" if unicode else "+")
        assert all(
            line.startswith("│" if unicode else "|") and line.endswith("│" if unicode else "|")
            for line in lines[1:-1]
        )


def test_panel_titles_are_literal_and_explicit_width_is_capped() -> None:
    component = panel("[red]content[/red]", title="[link]VERDICT[/link]", width=200, mode=ACTIVE)
    output = io.StringIO()
    Console(file=output, width=60, height=200, force_terminal=False, color_system=None).print(
        component
    )
    assert "[link]VERDICT[/link]" in output.getvalue()
    assert "[red]content[/red]" in output.getvalue()
    assert all(Text(line).cell_len == 60 for line in output.getvalue().splitlines())


def test_synchronized_output_releases_on_exception() -> None:
    output = TTY()
    with pytest.raises(RuntimeError), synchronized_output(output, mode=ACTIVE, supported=True):
        output.write("frame")
        raise RuntimeError("render failed")
    assert output.getvalue() == SYNC_START + "frame" + SYNC_END


@pytest.mark.parametrize("env", [{}, {"NO_COLOR": ""}, {"VERDICT_NO_ANIMATION": "1"}])
def test_sync_fallback_has_no_controls(env: dict[str, str]) -> None:
    output = TTY()
    with synchronized_output(output, env=env):
        output.write("frame")
    assert output.getvalue() == "frame"


def test_sync_capability_is_conservative() -> None:
    assert supports_synchronized_output({"TERM": "xterm-kitty"})
    assert supports_synchronized_output({"TERM_PROGRAM": "WezTerm"})
    assert not supports_synchronized_output({"TERM": "xterm-kitty", "TMUX": "1"})
    assert not supports_synchronized_output({"TERM": "xterm-256color"})
    output = TTY()
    with synchronized_output(output, env={"TERM": "xterm-kitty"}):
        output.write("frame")
    assert output.getvalue() == SYNC_START + "frame" + SYNC_END


@pytest.mark.parametrize("module", ["design.py", "motion.py"])
def test_modules_have_no_sleep_calls(module: str) -> None:
    tree = ast.parse((Path(__file__).parents[1] / "verdict" / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "sleep")
            assert not (isinstance(node.func, ast.Name) and node.func.id == "sleep")
        if isinstance(node, ast.ImportFrom):
            assert all(alias.name != "sleep" for alias in node.names)


def luminance(rgb: tuple[int, int, int]) -> float:
    linear = [
        (channel / 255 / 12.92)
        if channel / 255 <= 0.04045
        else ((channel / 255 + 0.055) / 1.055) ** 2.4
        for channel in rgb
    ]
    return sum(
        channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


@pytest.mark.parametrize("surface", ["BACKGROUND", "SURFACE"])
def test_wcag_contrast_on_charcoal(surface: str) -> None:
    background = luminance(PALETTE[surface].rgb)
    assert (luminance(PALETTE["TEXT"].rgb) + 0.05) / (background + 0.05) >= 7
    for accent in ("PURPLE", "CYAN", "SUCCESS", "AMBER", "RED"):
        assert (luminance(PALETTE[accent].rgb) + 0.05) / (background + 0.05) >= 4.5


# Byte captures taken before edits, origin/main 4073a1840219cc2870bd363938d87e4fd2a6aba4.
def capture_plain_screens(width: int, condition: str) -> str:
    env = (
        {"NO_COLOR": ""}
        if condition == "no_color"
        else {"VERDICT_PLAIN": "1"}
        if condition == "plain"
        else {}
    )
    output = io.StringIO()
    with patch.dict(os.environ, env, clear=True):
        console = Console(
            file=output,
            width=width,
            height=200,
            force_terminal=condition != "non_tty",
            color_system="truecolor",
        )
        ui = TerminalUI(console)
        ui.header("Setup")
        ui.status("Python", "found", "Ready")
        ui.status("Worker", "failed", "quota exhausted")
        ui.panel("Cooldown", "cc/model: 60 seconds", tone="COOLDOWN")
        ui.console.print(
            render_home(
                HomeState(gateway="http://gw", gateway_ok=True, gateway_models=42),
                plain=True,
                width=width,
            )
        )
        events = [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:01+00:00",
                "type": "run_started",
                "data": {"goal": "Ship feature"},
            },
            {
                "seq": 2,
                "at": "2026-01-01T00:00:02+00:00",
                "type": "plan_ready",
                "data": {
                    "nodes": [{"node_id": "N1", "objective": "build"}],
                    "layers": [["N1"]],
                    "topology": "SOLO",
                },
            },
            {
                "seq": 3,
                "at": "2026-01-01T00:00:03+00:00",
                "type": "node_state",
                "node_id": "N1",
                "data": {"state": "VALIDATED"},
            },
            {
                "seq": 4,
                "at": "2026-01-01T00:00:04+00:00",
                "type": "run_finished",
                "data": {"outcome": "COMPLETE", "reason": "verified"},
            },
        ]
        output.write(render_text(events, width=width, plain=True))
    return output.getvalue()


@pytest.mark.parametrize("condition", ["plain", "no_color", "non_tty"])
@pytest.mark.parametrize("width", [60, 100, 200])
def test_existing_screens_plain_bytes_match_origin_main(width: int, condition: str) -> None:
    expected = {
        60: "676ff78e96addbeecca4e55f97a0d77f5104b52f548b4ec6739f9521ce505122",
        100: "caa59f5dd1ded86cd9927b6b9d5cfffd3f9c7f5c7da76df7b0fb491cd38e65fb",
        200: "caa59f5dd1ded86cd9927b6b9d5cfffd3f9c7f5c7da76df7b0fb491cd38e65fb",
    }
    output = capture_plain_screens(width, condition)
    assert "\x1b" not in output
    assert hashlib.sha256(output.encode()).hexdigest() == expected[width]


@pytest.mark.parametrize(
    "env,tty",
    [
        ({"NO_COLOR": ""}, True),
        ({"CI": "true"}, True),
        ({"VERDICT_PLAIN": "1"}, True),
        ({"TERM": "dumb"}, True),
        ({"VERDICT_REDUCED_MOTION": "1"}, True),
        ({"VERDICT_NO_ANIMATION": "1"}, True),
        ({}, False),
    ],
)
def test_supported_sync_still_obeys_presentation_policy(env: dict[str, str], tty: bool) -> None:
    output = TTY() if tty else io.StringIO()
    with synchronized_output(output, supported=True, env=env):
        output.write("frame")
    assert output.getvalue() == "frame"


def test_disabled_helpers_default_to_presentation_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_NO_ANIMATION", "1")
    assert MotionClock().phase() == 0
    assert pulse(0.5, state="running") == 1
    assert border_highlight(0.5, 80, state="running") is None
    assert trace_step(0.5, [("A", "B")]) is None


def test_state_colours_stay_distinct_when_rich_downgrades_to_16_colours() -> None:
    """Screens pass truecolor TOKENS to Rich; Rich downgrades them on 16-colour terminals.

    Cooldown/warning must not collapse onto failure (or success) after that downgrade.
    """
    from rich.color import Color, ColorSystem

    from verdict.design import PALETTE, TOKEN_ALIASES

    def ansi16(name: str) -> int | None:
        token = PALETTE[TOKEN_ALIASES.get(name, name)]
        return Color.parse(token.hex).downgrade(ColorSystem.STANDARD).number

    failure, cooldown, success = ansi16("ERROR"), ansi16("COOLDOWN"), ansi16("SUCCESS")
    assert len({failure, cooldown, success}) == 3, (failure, cooldown, success)
    assert ansi16("WARNING") == cooldown


def test_terminal_ui_renders_cooldown_and_error_differently_in_16_colours() -> None:
    import io

    from rich.console import Console
    from rich.theme import Theme

    from verdict.design import TOKENS

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", theme=Theme(TOKENS))
    console.print("c", style="COOLDOWN", end="")
    cooldown = buf.getvalue()
    buf.seek(0)
    buf.truncate()
    console.print("e", style="ERROR", end="")
    error = buf.getvalue()
    strip = lambda s: s.replace("c", "").replace("e", "").replace("[0m", "").replace("\x1b", "")  # noqa: E731
    assert strip(cooldown).replace("1;", "") != strip(error).replace("1;", ""), (cooldown, error)
