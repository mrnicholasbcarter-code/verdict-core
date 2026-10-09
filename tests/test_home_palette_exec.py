"""`verdict` home command prompt tests.

Tests for the interactive command prompt that replaces the old arrow-key selector.
All tests use the key_reader injection (char-at-a-time) which _interactive_palette
wraps into a line reader.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from verdict.actions.registry import LaunchSpec
from verdict.home import (
    PALETTE,
    _call_launch_entry,
    _interactive_palette,
    _render_action_result,
    palette_launches,
)
from verdict.terminal_ui import TerminalUI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _console(width: int = 110, terminal: bool = True) -> Console:
    return Console(
        file=io.StringIO(),
        width=width,
        height=200,
        force_terminal=terminal,
        color_system="truecolor" if terminal else None,
    )


def _plain_console(width: int = 110) -> Console:
    return Console(
        file=io.StringIO(), width=width, height=200, force_terminal=False, color_system=None
    )


def _make_key_reader(*keys: str):
    """Return a callable that yields keys one at a time, then raises EOFError.

    Raising EOFError (not returning 'q' forever) prevents any loop that reads
    from this reader from spinning unboundedly when the scripted sequence is
    exhausted.
    """
    seq = list(keys)
    idx = [0]

    def reader() -> str:
        if idx[0] < len(seq):
            ch = seq[idx[0]]
            idx[0] += 1
            return ch
        raise EOFError("scripted key sequence exhausted")

    return reader


def _type_line(*chars: str) -> list[str]:
    """Helper: chars for typing a line and pressing Enter."""
    return [*list(chars), "\r"]


# ---------------------------------------------------------------------------
# 1. Command executes and calls run_action with the correct name
# ---------------------------------------------------------------------------


def test_command_calls_run_action_with_correct_name() -> None:
    """Typing a command name and pressing Enter calls run_palette_action."""
    console = _console()
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (True, {"status": "ok"})
        # Type "config" + Enter, then Ctrl-D to exit
        reader = _make_key_reader(*_type_line("c", "o", "n", "f", "i", "g"), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_rpa.assert_called_once()
    call_args = mock_rpa.call_args
    assert call_args[0][0] == "config.show"


def test_slash_command_calls_run_action() -> None:
    """Typing /config also works."""
    console = _console()
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (True, {"status": "ok"})
        reader = _make_key_reader(*_type_line("/", "c", "o", "n", "f", "i", "g"), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_rpa.assert_called_once()
    assert mock_rpa.call_args[0][0] == "config.show"


def test_command_renders_result_fields() -> None:
    """After running a command, output contains fields from the result."""
    console = _console(width=110)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (
            True,
            {"gateway": "http://test-gw", "exists": True, "profile": "default"},
        )
        reader = _make_key_reader(*_type_line("c", "o", "n", "f", "i", "g"), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "gateway" in output
    assert "http://test-gw" in output


def test_command_ok_false_renders_error() -> None:
    """ok=False renders the error panel."""
    console = _console(width=110)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (False, {"error": "something went wrong"})
        reader = _make_key_reader(*_type_line("c", "o", "n", "f", "i", "g"), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "something went wrong" in output


# ---------------------------------------------------------------------------
# 2. Required params are prompted and passed correctly
# ---------------------------------------------------------------------------


def test_required_param_is_passed_to_run_action() -> None:
    """Action with a required param: reader provides the value inline."""
    console = _console(width=110)

    called_with: list[dict[str, Any]] = []

    def fake_rpa(name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
        called_with.append({"name": name, "params": params or {}})
        return True, {"task": (params or {}).get("task"), "model": "gpt-4o"}

    with patch("verdict.home.run_palette_action", side_effect=fake_rpa):
        # Type "route" + Enter → prompts for task → type "my-task" + Enter
        # Then prompts for criticality → just Enter (accept default)
        # Then Ctrl-D to exit
        reader = _make_key_reader(
            *_type_line("r", "o", "u", "t", "e"),
            *_type_line("m", "y", "-", "t", "a", "s", "k"),
            *_type_line(),  # accept default criticality
            "\x04",
        )
        _interactive_palette(console, MagicMock(), key_reader=reader)

    assert called_with, "run_palette_action should have been called"
    assert "task" in called_with[0]["params"]
    assert called_with[0]["params"]["task"] == "my-task"


def test_required_param_cancel_on_empty() -> None:
    """Pressing Enter on a required param (empty) cancels the action."""
    console = _console(width=110)

    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (True, {"ok": True})
        # Type "route" + Enter → prompted for task → Enter (empty) → cancel
        reader = _make_key_reader(
            *_type_line("r", "o", "u", "t", "e"),
            *_type_line(),  # empty required param → cancel
            "\x04",
        )
        _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_rpa.assert_not_called()
    output = console.file.getvalue()
    assert "cancelled" in output.lower()


# ---------------------------------------------------------------------------
# 3. LAUNCH entries: monkeypatched LAUNCH with LaunchSpec shape
# ---------------------------------------------------------------------------


def test_launch_entry_calls_entry_in_process() -> None:
    """_call_launch_entry invokes a pkg.module:function in-process."""
    fake_mod = types.ModuleType("_fake_launch_mod")
    captured_kwargs: list[dict[str, Any]] = []

    def fake_entry(**kwargs: Any) -> int:
        captured_kwargs.append(kwargs)
        return 0

    fake_mod.fake_entry = fake_entry  # type: ignore[attr-defined]
    sys.modules["_fake_launch_mod"] = fake_mod

    try:
        ok, _data = _call_launch_entry("_fake_launch_mod:fake_entry", {"goal": "test"})
        assert ok is True
        assert captured_kwargs == [{"goal": "test"}]
    finally:
        del sys.modules["_fake_launch_mod"]


def test_launch_spec_shape_is_used_when_present() -> None:
    """LAUNCH dict[str, LaunchSpec]: entry is resolved and called via command."""
    fake_mod = types.ModuleType("_fake_launch2")
    results: list[str] = []

    def fake_fn(**kwargs: Any) -> int:
        results.append("called")
        return 0

    fake_mod.fake_fn = fake_fn  # type: ignore[attr-defined]
    sys.modules["_fake_launch2"] = fake_mod

    fake_launch_spec = LaunchSpec(
        reason="test launch", entry="_fake_launch2:fake_fn", section="Runs"
    )
    fake_launch = {"orchestrate": fake_launch_spec}

    console = _console(width=110)

    try:
        with (
            patch("verdict.actions.registry.LAUNCH", fake_launch),
            patch("verdict.home.palette_launches") as mock_pl,
            patch("verdict.home.palette_actions", return_value=[]),
        ):
            mock_pl.return_value = [("Runs", "orchestrate", "test desc", "orchestrate")]
            # Type "orchestrate" + Enter, then Ctrl-D
            reader = _make_key_reader(*_type_line(*list("orchestrate")), "\x04")
            _interactive_palette(console, MagicMock(), key_reader=reader)

        assert results == ["called"], f"Expected entry called, got: {results}"
    finally:
        del sys.modules["_fake_launch2"]


def test_launch_plain_string_shows_error() -> None:
    """Pre-lane-E LAUNCH shape (plain string): renders informative error."""
    fake_launch = {"orchestrate": "long-running orchestration pipeline"}
    console = _console(width=110)

    with (
        patch("verdict.actions.registry.LAUNCH", fake_launch),
        patch("verdict.home.palette_launches") as mock_pl,
        patch("verdict.home.palette_actions", return_value=[]),
    ):
        mock_pl.return_value = [("Runs", "orchestrate", "test desc", "orchestrate")]
        reader = _make_key_reader(*_type_line(*list("orchestrate")), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "orchestrate" in output.lower() or "launch" in output.lower()


# ---------------------------------------------------------------------------
# 4. _render_action_result unit tests
# ---------------------------------------------------------------------------


def test_render_list_of_dicts_produces_table() -> None:
    console = _console(width=110)
    tui = TerminalUI(console)
    data = [
        {"id": "m1", "provider": "openai", "tier": "frontier"},
        {"id": "m2", "provider": "anthropic", "tier": "standard"},
    ]
    _render_action_result(tui, True, data, width=110)
    output = console.file.getvalue()
    assert "m1" in output
    assert "openai" in output


def test_render_dict_produces_kv_panel() -> None:
    console = _console(width=110)
    tui = TerminalUI(console)
    data = {"gateway": "http://test-gw", "exists": True, "profile": "default"}
    _render_action_result(tui, True, data, width=110)
    output = console.file.getvalue()
    assert "gateway" in output
    assert "http://test-gw" in output


def test_render_ok_false_shows_error() -> None:
    console = _console(width=110)
    tui = TerminalUI(console)
    _render_action_result(tui, False, {"error": "fatal error msg"}, width=110)
    output = console.file.getvalue()
    assert "fatal error msg" in output


def test_render_ok_false_plain_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console = _plain_console(width=110)
    tui = TerminalUI(console)
    _render_action_result(tui, False, {"error": "plain error"}, width=110)
    output = console.file.getvalue()
    assert "\x1b[" not in output
    assert "plain error" in output


# ---------------------------------------------------------------------------
# 5. _call_launch_entry edge cases
# ---------------------------------------------------------------------------


def test_call_launch_entry_bad_format() -> None:
    ok, data = _call_launch_entry("no_colon_here", {})
    assert ok is False
    assert "invalid entry format" in data["error"]


def test_call_launch_entry_missing_module() -> None:
    ok, _data = _call_launch_entry("nonexistent.module:func", {})
    assert ok is False


def test_call_launch_entry_missing_function() -> None:
    ok, _data = _call_launch_entry("os:nonexistent_func_xyz", {})
    assert ok is False


def test_call_launch_entry_returns_action_result() -> None:
    """ActionResult-like return value is normalised."""
    fake = types.ModuleType("_fake_ar")

    class FakeResult:
        ok: bool = True
        data: dict[str, str] = {"status": "done"}  # noqa: RUF012

    fake.go = lambda **kw: FakeResult()  # type: ignore[attr-defined]
    sys.modules["_fake_ar"] = fake
    try:
        ok, result_data = _call_launch_entry("_fake_ar:go", {})
        assert ok is True
        assert result_data == {"status": "done"}
    finally:
        del sys.modules["_fake_ar"]


# ---------------------------------------------------------------------------
# 6. palette_launches uses LAUNCH registry
# ---------------------------------------------------------------------------


def test_palette_launches_returns_launch_entries() -> None:
    entries = palette_launches()
    for _section, cmd, _desc, launch_key in entries:
        assert launch_key == cmd


def test_palette_launches_monkeypatched() -> None:
    fake_launch_spec = LaunchSpec(reason="test", entry="x:y", section="R")
    with patch("verdict.actions.registry.LAUNCH", {"orchestrate": fake_launch_spec}):
        entries = palette_launches()
    assert any(e[1] == "orchestrate" for e in entries)


# ---------------------------------------------------------------------------
# 7. Ctrl-D exits cleanly (exit code 0)
# ---------------------------------------------------------------------------


def test_ctrl_d_exits_zero() -> None:
    """Ctrl-D exits the prompt with code 0."""
    console = _console()
    reader = _make_key_reader("\x04")
    result = _interactive_palette(console, MagicMock(), key_reader=reader)
    assert result == 0


def test_eof_from_exhausted_reader_exits_cleanly() -> None:
    """EOFError from exhausted reader exits cleanly."""
    console = _console()
    reader = _make_key_reader()  # empty → immediate EOFError
    result = _interactive_palette(console, MagicMock(), key_reader=reader)
    assert result == 0


# ---------------------------------------------------------------------------
# 8. config.show specific test (command prompt version)
# ---------------------------------------------------------------------------


def test_config_show_renders_output() -> None:
    """Typing 'config' renders config output."""
    console = _console(width=110)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (
            True,
            {"exists": True, "gateway": "http://127.0.0.1:20128", "profile": "default"},
        )
        reader = _make_key_reader(*_type_line(*list("config")), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "exists" in output and "True" in output


# ---------------------------------------------------------------------------
# 9. Unknown command gives a suggestion
# ---------------------------------------------------------------------------


def test_unknown_command_gives_suggestion() -> None:
    """Unknown /command shows 'did you mean' suggestion."""
    console = _console(width=110)
    reader = _make_key_reader(*_type_line(*list("/dmo")), "\x04")
    _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "unknown command" in output.lower()
    assert "did you mean /demo" in output.lower(), output


# ---------------------------------------------------------------------------
# 10. Free text asks for confirmation and does NOT launch on N or empty
# ---------------------------------------------------------------------------


def test_free_text_asks_confirmation_and_declines_on_empty() -> None:
    """Free text triggers goal confirmation. Empty answer does not launch."""
    console = _console(width=110)
    with patch("verdict.home._call_launch_entry") as mock_launch:
        mock_launch.return_value = (True, {})
        # Type "build a website" + Enter → confirmation prompt → Enter (empty = N) → Ctrl-D
        reader = _make_key_reader(
            *_type_line(*list("build a website")),
            *_type_line(),  # empty answer = No
            "\x04",
        )
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert (
        "orchestrate" in output.lower() or "goal" in output.lower() or "credits" in output.lower()
    )
    # Should NOT have launched
    mock_launch.assert_not_called()


def test_free_text_declines_on_n() -> None:
    """Explicit 'n' answer does not launch."""
    console = _console(width=110)
    with patch("verdict.home._call_launch_entry") as mock_launch:
        mock_launch.return_value = (True, {})
        reader = _make_key_reader(
            *_type_line(*list("build a website")),
            *_type_line("n"),  # explicit N
            "\x04",
        )
        _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_launch.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Ctrl-C during a command returns to the prompt
# ---------------------------------------------------------------------------


def test_ctrl_c_during_command_returns_to_prompt() -> None:
    """KeyboardInterrupt during action returns to prompt with 'cancelled'."""
    console = _console(width=110)

    def _raise_interrupt(name: str, params: Any) -> tuple[bool, dict[str, Any]]:
        raise KeyboardInterrupt("user pressed Ctrl-C")

    with patch("verdict.home.run_palette_action", side_effect=_raise_interrupt):
        # Type "config" + Enter → raises → should show cancelled → Ctrl-D
        reader = _make_key_reader(*_type_line(*list("config")), "\x04")
        result = _interactive_palette(console, MagicMock(), key_reader=reader)

    assert result == 0
    output = console.file.getvalue()
    assert "cancelled" in output.lower()


# ---------------------------------------------------------------------------
# 12. Narrow terminal does not crash
# ---------------------------------------------------------------------------


def test_narrow_terminal_does_not_crash() -> None:
    """Width=60 does not crash."""
    console = _console(width=60)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (
            True,
            [
                {"id": "model-a", "provider": "openai", "tier": "frontier"},
                {"id": "model-b", "provider": "anthropic", "tier": "standard"},
            ],
        )
        reader = _make_key_reader(*_type_line(*list("models")), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert output  # something was rendered


def test_narrow_terminal_table_renders() -> None:
    """Narrow terminal (width=60): list[dict] renders a table."""
    console = _console(width=60)
    tui = TerminalUI(console)
    data = [
        {"id": "model-x", "provider": "openai", "tier": "frontier", "extra": "ignored-in-narrow"},
        {"id": "model-y", "provider": "kr", "tier": "standard", "extra": "ignored-in-narrow"},
    ]
    _render_action_result(tui, True, data, width=60)
    output = console.file.getvalue()
    assert "model" in output


# ---------------------------------------------------------------------------
# 13. Exhausted key reader during param prompt cancels
# ---------------------------------------------------------------------------


def test_exhausted_key_reader_during_param_prompt_cancels() -> None:
    """EOFError during param prompting cancels gracefully."""
    console = _console(width=110)

    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (True, {"ok": True})
        # Type "route" + Enter, then reader exhausts during param typing
        reader = _make_key_reader(*_type_line(*list("route")), "a", "b", "c")
        result = _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_rpa.assert_not_called()
    assert result == 0


def test_make_key_reader_raises_eoferror_when_exhausted() -> None:
    """_make_key_reader raises EOFError after its sequence is exhausted."""
    reader = _make_key_reader("a", "b")
    assert reader() == "a"
    assert reader() == "b"
    with pytest.raises(EOFError):
        reader()


# ---------------------------------------------------------------------------
# 14. config.show integration test
# ---------------------------------------------------------------------------


def test_palette_real_action_integration_config_show(tmp_path: Path) -> None:
    """Integration test: real config.show action through prompt."""
    console = _console(width=110)

    config_dir = tmp_path / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "verdict.yaml"
    config_file.write_text(
        "providers:\n  openai:\n    api_key: test-key-12345\n    base_url: https://api.openai.com\n"
    )

    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path / ".config")}):
        reader = _make_key_reader(*_type_line(*list("config")), "\x04")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    rendered = console.file.getvalue()
    assert "exists" in rendered and "True" in rendered
    assert "openai" in rendered
    assert "test-key-12345" not in rendered


# ---------------------------------------------------------------------------
# 15. KeyboardInterrupt during launch returns to prompt
# ---------------------------------------------------------------------------


def test_launch_entry_keyboard_interrupt_returns_to_prompt(tmp_path: Path) -> None:
    """KeyboardInterrupt during action execution shows 'cancelled'."""
    console = _console(width=110)

    def _raise_interrupt(ref: str, params: Any) -> tuple[bool, dict[str, Any]]:
        raise KeyboardInterrupt("user pressed Ctrl-C")

    with patch("verdict.home.run_palette_action", side_effect=_raise_interrupt):
        reader = _make_key_reader(*_type_line(*list("config")), "\x04")
        result = _interactive_palette(console, MagicMock(), key_reader=reader)

    assert result == 0
    output = console.file.getvalue()
    assert "cancelled" in output.lower()


# ---------------------------------------------------------------------------
# 16. config.show redacts secrets
# ---------------------------------------------------------------------------


def test_config_show_action_redacts_secrets(tmp_path, monkeypatch) -> None:
    """config.show must not return credentials held in verdict.yaml."""
    from verdict.actions import run_action

    cfg = tmp_path / "verdict"
    cfg.mkdir()
    (cfg / "verdict.yaml").write_text(
        "providers:\n  openai:\n    api_key: sk-live-abc\n    base_url: https://x\n"
        "auth:\n  token: tok-1\n  password: pw-1\n  client_secret: cs-1\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    res = run_action("config.show")
    flat = repr(res.data)
    for secret in ("sk-live-abc", "tok-1", "pw-1", "cs-1"):
        assert secret not in flat
    assert res.data["config"]["providers"]["openai"]["base_url"] == "https://x"


def test_config_show_parse_error_does_not_echo_file_content(tmp_path, monkeypatch) -> None:
    """When verdict.yaml is malformed, config.show must not echo the raw file."""
    from verdict.actions import run_action

    cfg = tmp_path / "verdict"
    cfg.mkdir()
    (cfg / "verdict.yaml").write_text("SECRET_KEY: sk-live-leaked-secret\n  bad_indent: oops\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    res = run_action("config.show")
    flat = repr(res.data)
    assert "sk-live-leaked-secret" not in flat


def test_configuration_home_uses_registered_redacted_action() -> None:
    """The PALETTE entry for config maps to the registered config.show action."""
    config_entries = [(g, c, d, a) for g, c, d, a in PALETTE if c == "config"]
    assert config_entries, "config must be in PALETTE"
    assert config_entries[0][3] == "config.show"


# ---------------------------------------------------------------------------
# 17. Completion lists every registered command
# ---------------------------------------------------------------------------


def test_completion_includes_all_palette_commands() -> None:
    """VerdictCompleter in prompt_toolkit yields completions for all PALETTE commands."""
    from verdict.home import _get_command_index

    index = _get_command_index()
    for _, cmd, _, _ in PALETTE:
        assert cmd in index or f"/{cmd}" in index, f"Missing command: {cmd}"


# ---------------------------------------------------------------------------
# 18. History is written and capped
# ---------------------------------------------------------------------------


def test_history_file_operations(tmp_path: Path) -> None:
    """History load/save/cap work correctly."""
    from verdict.home import HISTORY_MAX_LINES, _load_history, _save_history

    hist_file = tmp_path / "test_history"

    # Save some entries
    _save_history(hist_file, ["cmd1", "cmd2", "cmd3"])
    loaded = _load_history(hist_file)
    assert loaded == ["cmd1", "cmd2", "cmd3"]

    # Cap works
    _save_history(hist_file, [f"line{i}" for i in range(HISTORY_MAX_LINES + 10)])
    loaded = _load_history(hist_file)
    assert len(loaded) <= HISTORY_MAX_LINES


# ---------------------------------------------------------------------------
# 19. Motion: animate=False produces no frames / fast startup
# ---------------------------------------------------------------------------


def test_startup_motion_fast_probe_completes_quickly() -> None:
    """With a fast probe, startup completes in under 0.3s with animation."""
    import time

    from verdict.home import HomeState, _startup_with_motion

    console = _console(terminal=True)
    state = HomeState(gateway="http://test")

    def fast_probe() -> tuple[bool, int, None]:
        return True, 5, None

    start = time.monotonic()
    _startup_with_motion(console, state, probe_fn=fast_probe, max_sweep_s=1.2)
    elapsed = time.monotonic() - start

    assert elapsed < 0.3, f"Startup took {elapsed:.2f}s, expected < 0.3s"
    output = console.file.getvalue()
    assert "REACHABLE" in output or "gateway" in output


def test_startup_motion_slow_probe_stops_after_probe() -> None:
    """With a 0.5s probe, sweep stops within ~0.1s after the probe returns."""
    import time

    from verdict.home import HomeState, _startup_with_motion

    console = _console(terminal=True)
    state = HomeState(gateway="http://test")

    def slow_probe() -> tuple[bool, int, None]:
        time.sleep(0.5)
        return True, 42, None

    start = time.monotonic()
    _startup_with_motion(console, state, probe_fn=slow_probe, max_sweep_s=1.2)
    elapsed = time.monotonic() - start

    assert elapsed < 0.8, f"Startup took {elapsed:.2f}s, expected < 0.8s (0.5s probe + 0.1s)"
    output = console.file.getvalue()
    assert "42" in output or "REACHABLE" in output


# ---------------------------------------------------------------------------
# 20. /help shows all commands
# ---------------------------------------------------------------------------


def test_help_command_shows_all_groups() -> None:
    """Typing 'help' shows command groups."""
    console = _console(width=110)
    reader = _make_key_reader(*_type_line(*list("help")), "\x04")
    _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    # Should show at least some command groups
    assert "config" in output.lower() or "setup" in output.lower()
    assert "quit" in output.lower()


# ---------------------------------------------------------------------------
# 21. NO_COLOR: no ANSI escapes
# ---------------------------------------------------------------------------


def test_no_color_has_no_ansi(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NO_COLOR environment: output must contain no ANSI escape sequences."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("CI", "1")
    console = _plain_console(width=110)
    from verdict.home import run_home

    run_home(console=console, runs_roots=[tmp_path], probe=False, animate=False)
    output = console.file.getvalue()
    assert "\x1b[" not in output


# ---------------------------------------------------------------------------
# Review round 1 (cx/gpt-6-sol at 4c8d5ac)
# ---------------------------------------------------------------------------


def test_startup_probe_that_raises_does_not_hang() -> None:
    """A probe exception is a failed probe; startup still completes promptly."""
    import time

    from verdict.home import HomeState, _startup_with_motion

    console = _console(terminal=True)
    state = HomeState(gateway="http://test")

    def broken_probe() -> tuple[bool, int, None]:
        raise RuntimeError("boom")

    start = time.monotonic()
    _startup_with_motion(console, state, probe_fn=broken_probe, max_sweep_s=0.3)
    assert time.monotonic() - start < 2.0
    assert state.gateway_ok is False
    assert "UNREACHABLE" in console.file.getvalue()


def test_probe_gateway_read_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized /v1/models response is a failed probe, not an unbounded read."""
    import verdict.home as home

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.asked: list[int] = []

        def read(self, n: int = -1) -> bytes:
            self.asked.append(n)
            return self.body if n < 0 else self.body[:n]

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr(home, "_PROBE_MAX_BYTES", 64)
    big = _Resp(b'{"data": [' + b'{"id": "m"},' * 50 + b'{"id": "m"}]}')

    class _Opener:
        def __init__(self, response: _Resp) -> None:
            self.response = response

        def open(self, *args: object, **kwargs: object) -> _Resp:
            return self.response

    monkeypatch.setattr(home.urllib.request, "build_opener", lambda *a: _Opener(big))
    assert home.probe_gateway("http://example.test") == (False, None, None)
    assert big.asked and all(n > 0 for n in big.asked), "read must be bounded"
    small = _Resp(b'{"data": [{"id": "a"}, {"id": "b"}]}')
    monkeypatch.setattr(home.urllib.request, "build_opener", lambda *a: _Opener(small))
    assert home.probe_gateway("http://example.test") == (True, 2, None)


def test_history_is_capped_and_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import stat

    import verdict.home as home

    path = tmp_path / "hist" / "prompt_history"
    monkeypatch.setattr(home, "HISTORY_MAX_LINES", 5)
    for i in range(12):
        home._save_history(path, [f"cmd{i}"])
    lines = path.read_text().splitlines()
    assert lines == [f"cmd{i}" for i in range(7, 12)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_failing_command_reports_error_and_returns_to_prompt() -> None:
    """An action that raises is shown as an error; the loop keeps running."""
    console = _console(width=110)
    with patch("verdict.home.run_palette_action", side_effect=ValueError("bad input")):
        reader = _make_key_reader(
            *_type_line(*list("/config")), *_type_line(*list("/config")), "\x04"
        )
        rc = _interactive_palette(console, MagicMock(), key_reader=reader)
    assert rc == 0
    out = console.file.getvalue()
    assert out.count("ValueError: bad input") == 2, out


def test_fallback_only_when_prompt_toolkit_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors inside the prompt_toolkit loop are not swallowed into the fallback."""
    import verdict.home as home

    calls: list[str] = []
    monkeypatch.setattr(home, "_prompt_toolkit_ready", lambda: None)

    def boom(*a: object) -> int:
        calls.append("ptk")
        raise RuntimeError("render bug")

    monkeypatch.setattr(home, "_prompt_toolkit_loop", boom)
    monkeypatch.setattr(home, "_fallback_input_loop", lambda *a: calls.append("fallback") or 0)
    with pytest.raises(RuntimeError):
        home._command_prompt(_console(terminal=True), home.HomeState())
    assert calls == ["ptk"]

    def not_ready() -> None:
        raise RuntimeError("no tty")

    calls.clear()
    monkeypatch.setattr(home, "_prompt_toolkit_ready", not_ready)
    assert home._command_prompt(_console(terminal=True), home.HomeState()) == 0
    assert calls == ["fallback"]


def test_history_reads_legacy_prompt_toolkit_format(tmp_path: Path) -> None:
    import verdict.home as home

    path = tmp_path / "prompt_history"
    path.write_text("\n# 2026-09-30 00:30:05\n+/demo\n\n# 2026-09-30 00:30:07\n+/runs\n/config\n")
    assert home._load_history(path) == ["/demo", "/runs", "/config"]


def test_startup_prints_the_whole_wordmark_when_the_probe_is_fast() -> None:
    """A probe that finishes first stops the pacing, not the wordmark."""
    from verdict.home import WORDMARK, HomeState, _startup_with_motion

    console = _console(terminal=True)
    _startup_with_motion(console, HomeState(gateway="http://t"), probe_fn=lambda: (True, 3, None))
    out = console.file.getvalue()
    for line in WORDMARK:
        assert line.rstrip() in out


# ---------------------------------------------------------------------------
# Real terminal: the production prompt_toolkit path in a pseudo-terminal
# ---------------------------------------------------------------------------

_PTY_CHILD = """
import os, sys
os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"
import verdict.home as home

calls = {"n": 0}

def failing_action(name, params=None):
    calls["n"] += 1
    raise ValueError("pty boom %d" % calls["n"])

home.run_palette_action = failing_action


def _no_fallback(*args, **kwargs):
    raise SystemExit("FALLBACK LOOP USED")


# The test is about the prompt_toolkit session: fail loudly if the plain
# input() fallback is used instead.
home._fallback_input_loop = _no_fallback
sys.exit(home.run_home(probe=False))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty only")
def test_real_prompt_session_survives_a_failing_command(tmp_path: Path) -> None:
    """Drive the real prompt_toolkit session in a pty: a raising command shows
    its error, the prompt comes back, a second command runs, Ctrl-D exits 0."""
    import os
    import pty
    import select
    import time

    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "child.py"
    script.write_text(_PTY_CHILD)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(root),
        "TERM": "xterm-256color",
        "COLUMNS": "100",
        "LINES": "40",
    }
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        os.chdir(tmp_path)
        os.execve(sys.executable, [sys.executable, str(script)], env)

    buf = b""

    def pump(seconds: float, until: bytes | None = None) -> None:
        nonlocal buf
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                if until is not None and buf.count(until) >= 1:
                    return

    try:
        pump(20.0, until="verdict ›".encode())  # noqa: RUF001
        assert "verdict ›".encode() in buf, buf[-2000:]  # noqa: RUF001
        os.write(fd, b"/config\r")
        pump(10.0, until=b"pty boom 1")
        os.write(fd, b"/config\r")
        pump(10.0, until=b"pty boom 2")
        # Wait until the prompt is drawn again before sending Ctrl-D.
        mark = len(buf)
        end = time.monotonic() + 10.0
        while time.monotonic() < end and "verdict ›".encode() not in buf[mark:]:  # noqa: RUF001
            pump(0.2)
        pump(0.3)
        os.write(fd, b"\x04")
        status = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            pump(0.2)
            done_pid, st = os.waitpid(pid, os.WNOHANG)
            if done_pid == pid:
                status = st
                break
        assert status is not None, (
            "prompt did not exit on Ctrl-D: " + buf.decode("utf-8", "replace")[-1500:]
        )
    finally:
        import contextlib

        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)
        os.close(fd)
    text = buf.decode("utf-8", "replace")
    assert "ValueError: pty boom 1" in text, text[-3000:]
    assert "ValueError: pty boom 2" in text, text[-3000:]
    assert "Traceback" not in text
    assert "FALLBACK LOOP USED" not in text
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def test_history_tightens_existing_permissions(tmp_path: Path) -> None:
    import stat

    import verdict.home as home

    d = tmp_path / "shared"
    d.mkdir(mode=0o777)
    d.chmod(0o777)
    path = d / "prompt_history"
    path.write_text("/old\n")
    path.chmod(0o644)
    home._save_history(path, ["/new"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert path.read_text().splitlines() == ["/old", "/new"]


def test_probe_gateway_malformed_url_never_raises() -> None:
    from verdict.home import probe_gateway

    assert probe_gateway("http://[::1") == (None, None, None)
    assert probe_gateway("ftp://example.test") == (None, None, None)


def _load_recorder() -> types.ModuleType:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "record_home_prompt", root / "scripts" / "record_home_prompt.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recorder_rejects_a_capture_missing_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _load_recorder()
    good_text = (
        "Checking gateway reachability\ngateway  REACHABLE  http://x (5 models)\n"
        "verdict › CLAIMS VERIFIED Run orchestrate on this goal?"  # noqa: RUF001
    )
    good = [(0.1, good_text.encode())]
    assert rec.recording_problems(good) == []
    missing = [(0.1, good_text.replace("CLAIMS VERIFIED", "").encode())]
    assert rec.recording_problems(missing) == ["missing 'CLAIMS VERIFIED'"]
    unreachable = [(0.1, good_text.replace("gateway  REACHABLE", "gateway  UNREACHABLE").encode())]
    assert rec.recording_problems(unreachable) == ["gateway not REACHABLE"]
    styled = good_text.replace(
        "gateway  REACHABLE", "\x1b[38;2;161;161;170mgateway  \x1b[0m\x1b[38;2;74;222;128mREACHABLE"
    )
    assert rec.recording_problems([(0.1, styled.encode())]) == []
    no_checking = [(0.1, good_text.replace("Checking gateway reachability", "").encode())]
    assert rec.recording_problems(no_checking) == ["missing 'Checking gateway reachability'"]
    crashed = [(0.1, good[0][1] + b" Traceback (most recent call last)")]
    assert "traceback in output" in rec.recording_problems(crashed)

    out = tmp_path / "docs" / "assets" / "home-prompt.cast"
    monkeypatch.setattr(rec, "ROOT", tmp_path)
    monkeypatch.setattr(rec, "read_pty_with_input", lambda *a, **k: missing)
    assert rec.main() == 1
    assert not out.exists(), "an incomplete capture must not be written"


def test_recorder_redacts_cwd_and_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _load_recorder()
    home = tmp_path / "home" / "someone"
    monkeypatch.setenv("HOME", str(home))
    cwd = "/srv/build/work-dir"
    chunks = [(0.1, f"repo: {cwd}\nconfig {home}/.config/verdict/verdict.yaml".encode())]
    text = rec.sanitize_chunks(chunks, cwd=cwd)[0][1].decode()
    assert cwd not in text and str(home) not in text
    assert "repo: ." in text and "~/.config/verdict/verdict.yaml" in text


def test_recorder_redacts_a_path_split_across_pty_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _load_recorder()
    home = "/home/someone"
    monkeypatch.setenv("HOME", home)
    cwd = "/srv/build/work-dir"
    stream = f"repo: {cwd}\nconfig {home}/.config/verdict/verdict.yaml\n".encode()
    for cut in range(1, len(stream)):
        chunks = [(0.1, stream[:cut]), (0.2, stream[cut:])]
        out = rec.sanitize_chunks(chunks, cwd=cwd)
        assert [t for t, _ in out] == [0.1, 0.2]
        text = b"".join(data for _, data in out).decode()
        assert cwd not in text and home not in text, (cut, text)
        assert text == "repo: .\nconfig ~/.config/verdict/verdict.yaml\n"


def test_bootstrap_palette_uses_structured_controller_not_generic_first_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.actions.base import ActionResult
    from verdict.home import HomeState, _run_command

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    console = _plain_console(180)
    with (
        patch(
            "verdict.tui_bootstrap_controls.consume_prime",
            return_value=ActionResult(data={"status": "cancelled"}),
        ) as prime,
        patch("verdict.home.run_palette_action") as generic,
    ):
        _run_command(
            "/bootstrap prime cc/a cc/b",
            tui=TerminalUI(console),
            state=HomeState(gateway="http://127.0.0.1:9"),
            line_reader=lambda: "n",
        )
    generic.assert_not_called()
    assert prime.call_args.args[0].ids == ("cc/a", "cc/b")
    assert [entry[1] for entry in PALETTE].count("bootstrap") == 1
    assert "setup" in [entry[1] for entry in PALETTE]
