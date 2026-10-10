"""First-run visual contracts. No live services and no unbounded input loops."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from verdict.design import PALETTE, PresentationMode
from verdict.home import HomeState, render_home, run_home
from verdict.terminal_ui import TerminalUI, _Elapsed

GOLDEN = Path(__file__).parent / "golden" / "first_run"


def setup_frame(ui: TerminalUI) -> None:
    ui.header("Setup")
    ui.bootstrap(
        {
            "stages": [{"stage": "discover", "status": "ok", "summary": "3 providers discovered"}],
            "providers": [
                {"provider_id": "gateway.local", "provider_kind": "gateway", "lifecycle": "healthy"}
            ],
            "recommendations": [
                {
                    "capability_id": "code.symbols",
                    "status": "covered",
                    "selected_provider_id": "native",
                    "reason": "Local semantic navigation",
                }
            ],
            "plan": {
                "actions": [
                    {
                        "kind": "preserve_config",
                        "description": "Keep existing gateway configuration",
                        "reason": "No changes needed",
                    }
                ],
                "plan_id": "setup-preview",
            },
            "certification": {
                "results": [
                    {
                        "provider_id": "gateway.local",
                        "certified": True,
                        "reason": "Observed health check passed",
                    }
                ]
            },
            "mutation_free": True,
        }
    )


def home_frame(console: Console, plain: bool) -> None:
    console.print(
        render_home(
            HomeState(
                gateway="http://localhost:20128",
                gateway_ok=True,
                gateway_models=42,
                runs=[
                    {
                        "run": "ship-feature",
                        "outcome": "COMPLETE",
                        "age_s": 120,
                        "reason": "Receipt verified",
                    }
                ],
            ),
            plain=plain,
            width=console.width,
        )
    )


@pytest.mark.parametrize("name", ["home", "setup"])
@pytest.mark.parametrize("width", [60, 100, 200])
@pytest.mark.parametrize("policy", ["plain", "no_color", "non_tty"])
def test_plain_golden(monkeypatch: pytest.MonkeyPatch, name: str, width: int, policy: str) -> None:
    for key in ("NO_COLOR", "CI", "VERDICT_PLAIN", "TERM"):
        monkeypatch.delenv(key, raising=False)
    if policy == "plain":
        monkeypatch.setenv("VERDICT_PLAIN", "1")
    if policy == "no_color":
        monkeypatch.setenv("NO_COLOR", "")
    output = io.StringIO()
    console = Console(
        file=output, width=width, force_terminal=policy != "non_tty", color_system="truecolor"
    )
    ui = TerminalUI(console)
    if name == "home":
        home_frame(ui.console, ui.plain)
    else:
        setup_frame(ui)
    text = output.getvalue()
    assert text == (GOLDEN / f"{name}-{width}.txt").read_text()
    assert "\x1b" not in text
    assert all(Text(line).cell_len <= width for line in text.splitlines())


@pytest.mark.parametrize("name", ["home", "setup"])
def test_color_tokens_and_complete_resized_borders(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    for key in ("NO_COLOR", "CI", "VERDICT_PLAIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    for width in (60, 100, 200, 60):
        output = io.StringIO()
        console = Console(file=output, width=width, force_terminal=True, color_system="truecolor")
        if name == "home":
            home_frame(console, False)
        else:
            setup_frame(TerminalUI(console))
        text = output.getvalue()
        for token in ("PURPLE", "SURFACE", "TEXT"):
            assert ";".join(map(str, PALETTE[token].rgb)) in text
        lines = [Text.from_ansi(line).plain for line in text.splitlines()]
        assert all(Text(line).cell_len <= width for line in lines)
        for raw_line in lines:
            line = raw_line.rstrip()
            if line.startswith("╭"):
                assert line.endswith("╮")
            if line.startswith("╰"):
                assert line.endswith("╯")
            if line.startswith("│"):
                assert line.endswith("│")


@pytest.mark.parametrize(
    "env",
    [
        {"VERDICT_NO_ANIMATION": "1"},
        {"VERDICT_REDUCED_MOTION": "1"},
        {"REDUCED_MOTION": "reduce"},
        {"NO_COLOR": ""},
        {"CI": "true"},
        {"TERM": "dumb"},
        {"VERDICT_PLAIN": "1"},
    ],
)
def test_home_explicit_animation_obeys_policy(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], tmp_path: Path
) -> None:
    for key in (
        "NO_COLOR",
        "CI",
        "VERDICT_PLAIN",
        "TERM",
        "VERDICT_NO_ANIMATION",
        "VERDICT_REDUCED_MOTION",
        "REDUCED_MOTION",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("verdict.home.probe_gateway", lambda url: (True, 42, None))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("disabled motion started Live")

    monkeypatch.setattr("verdict.terminal_ui._SynchronizedLive", forbidden)
    output = io.StringIO()
    run_home(
        console=Console(file=output, force_terminal=True),
        runs_roots=[tmp_path],
        animate=True,
        interactive=False,
    )
    assert "\x1b[?25" not in output.getvalue()


def test_home_motion_only_during_observed_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in (
        "NO_COLOR",
        "CI",
        "VERDICT_PLAIN",
        "TERM",
        "VERDICT_NO_ANIMATION",
        "VERDICT_REDUCED_MOTION",
        "REDUCED_MOTION",
    ):
        monkeypatch.delenv(key, raising=False)
    events: list[str] = []
    monkeypatch.setattr(TerminalUI, "start", lambda self, label: events.append("start"))
    monkeypatch.setattr(TerminalUI, "stop", lambda self: events.append("stop"))

    def probe(url: str) -> tuple[bool, int, None]:
        events.append("probe")
        return True, 42, None

    monkeypatch.setattr("verdict.home.probe_gateway", probe)
    console = Console(file=io.StringIO(), force_terminal=True)
    run_home(console=console, runs_roots=[tmp_path], interactive=False, skip_setup=True)
    assert events == ["start", "probe", "stop", "stop"]
    events.clear()
    run_home(
        console=console, runs_roots=[tmp_path], probe=False, interactive=False, skip_setup=True
    )
    assert events == []


def test_setup_journey_does_not_invent_doctor_or_proof() -> None:
    output = io.StringIO()
    ui = TerminalUI(Console(file=output, force_terminal=False, width=200))
    ui.event("discover", "ok", "3 providers", {})
    ui.setup_journey()
    assert "DISCOVER [OK]" in output.getvalue()
    assert "DOCTOR [NOT RUN]" in output.getvalue()
    assert "FIRST RUN / PROOF [NOT RUN]" in output.getvalue()


def test_observed_probe_border_survives_resize() -> None:
    component = _Elapsed("Checking gateway", PresentationMode(True, True, True, 100))
    for width in (60, 200, 60):
        output = io.StringIO()
        Console(file=output, width=width, height=200, color_system=None).print(component)
        lines = output.getvalue().splitlines()
        assert all(Text(line).cell_len == width for line in lines)
        assert lines[0].startswith("╭") and lines[0].endswith("╮")
        assert lines[-1].startswith("╰") and lines[-1].endswith("╯")


def test_observed_task_sync_frame_and_cursor_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "NO_COLOR",
        "CI",
        "VERDICT_PLAIN",
        "VERDICT_NO_ANIMATION",
        "VERDICT_REDUCED_MOTION",
        "REDUCED_MOTION",
        "TMUX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "xterm-kitty")
    output = io.StringIO()
    ui = TerminalUI(Console(file=output, width=60, height=200, force_terminal=True))
    with pytest.raises(RuntimeError), ui.task("Gateway probe"):
        raise RuntimeError("probe ended")
    text = output.getvalue()
    assert text.count("\x1b[?2026h") == text.count("\x1b[?2026l") > 0
    assert text.rfind("\x1b[?25h") > text.find("\x1b[?25l") >= 0


def test_home_has_no_decorative_waits() -> None:
    import ast
    import inspect

    import verdict.home

    tree = ast.parse(inspect.getsource(verdict.home))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sleep"
        for node in ast.walk(tree)
    )
