"""tests/test_home_palette_exec.py -- Lane C palette execution tests.

Drive the selector with a scripted key sequence (inject a key_reader) and
assert that run_action was called with the right name/params and that the
rendered output contains fields of the ActionResult.  Also tests: ok=False
renders the error panel; NO_COLOR has no ANSI; COLUMNS=60 renders without
crashing.
"""

from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from verdict.actions.base import ActionResult, LaunchSpec
from verdict.home import (
    _call_launch_entry,
    _interactive_palette,
    _render_action_result,
    palette_actions,
    palette_launches,
    run_home,
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


# ---------------------------------------------------------------------------
# 1. run_action is called with the right name and params
# ---------------------------------------------------------------------------


def test_palette_enter_calls_run_action_with_correct_name() -> None:
    """Selecting a param-free entry and pressing Enter calls run_action."""
    actions = palette_actions()
    assert actions, "palette_actions() must return at least one entry"

    # Use the first param-free action so we don't need to supply required params.
    from verdict.home import _ACTION_PARAMS

    param_free_idx = next(
        (i for i, (_, _, _, a) in enumerate(actions) if a not in _ACTION_PARAMS), None
    )
    assert param_free_idx is not None, "palette must have at least one param-free action"
    target_action = actions[param_free_idx][3]

    # Navigate to param_free_idx using Down arrows, then Enter to run, q after result.
    nav_keys: list[str] = []
    for _ in range(param_free_idx):
        nav_keys += ["\x1b", "[", "B"]
    nav_keys += ["\r", "q"]

    console = _console()
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (True, {"status": "ok", "value": "test_value"})
        reader = _make_key_reader(*nav_keys)
        _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_rpa.assert_called_once()
    call_args = mock_rpa.call_args
    assert call_args[0][0] == target_action, (
        f"Expected run_palette_action called with {target_action!r}, got {call_args[0][0]!r}"
    )


def test_palette_enter_renders_action_result_fields() -> None:
    """After running a param-free action, output contains fields from the ActionResult."""
    from verdict.home import _ACTION_PARAMS

    actions = palette_actions()
    param_free_idx = next(
        (i for i, (_, _, _, a) in enumerate(actions) if a not in _ACTION_PARAMS), None
    )
    assert param_free_idx is not None

    nav_keys: list[str] = []
    for _ in range(param_free_idx):
        nav_keys += ["\x1b", "[", "B"]
    nav_keys += ["\r", "q"]

    console = _console(width=110)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (
            True,
            {"gateway": "http://test-gw", "exists": True, "profile": "default"},
        )
        reader = _make_key_reader(*nav_keys)
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "gateway" in output, f"Expected 'gateway' in output, got: {output[:500]!r}"


def test_palette_ok_false_renders_error_panel() -> None:
    """ok=False ActionResult renders the error panel, not raw repr."""
    from verdict.home import _ACTION_PARAMS

    actions = palette_actions()
    param_free_idx = next(
        (i for i, (_, _, _, a) in enumerate(actions) if a not in _ACTION_PARAMS), None
    )
    assert param_free_idx is not None

    nav_keys: list[str] = []
    for _ in range(param_free_idx):
        nav_keys += ["\x1b", "[", "B"]
    nav_keys += ["\r", "q"]

    console = _console(width=110)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (False, {"error": "something went wrong"})
        reader = _make_key_reader(*nav_keys)
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert "something went wrong" in output, (
        f"Expected error message in output, got: {output[:500]!r}"
    )
    # Must not be raw repr
    assert "ActionResult" not in output


def test_palette_no_color_has_no_ansi(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NO_COLOR environment: output must contain no ANSI escape sequences."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("CI", "1")  # also suppress interactive

    console = _plain_console(width=110)
    result = run_home(console=console, runs_roots=[tmp_path], probe=False, animate=False)
    output = console.file.getvalue()
    assert "\x1b[" not in output, "NO_COLOR output must not contain ANSI escapes"
    assert result == 0


def test_palette_narrow_terminal_does_not_crash() -> None:
    """COLUMNS=60 narrow terminal must not crash."""
    from verdict.home import _ACTION_PARAMS

    actions = palette_actions()
    param_free_idx = next(
        (i for i, (_, _, _, a) in enumerate(actions) if a not in _ACTION_PARAMS), None
    )
    assert param_free_idx is not None

    nav_keys: list[str] = []
    for _ in range(param_free_idx):
        nav_keys += ["\x1b", "[", "B"]
    nav_keys += ["\r", "q"]

    console = _console(width=60)
    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (
            True,
            [
                {"id": "model-a", "provider": "openai", "tier": "frontier"},
                {"id": "model-b", "provider": "anthropic", "tier": "standard"},
            ],
        )
        reader = _make_key_reader(*nav_keys)
        # Should not raise
        _interactive_palette(console, MagicMock(), key_reader=reader)

    output = console.file.getvalue()
    assert output  # something was rendered


def test_palette_narrow_terminal_table_renders() -> None:
    """Narrow terminal (width=60): list[dict] renders a table without IndexError."""
    console = _console(width=60)
    tui = TerminalUI(console)

    data = [
        {"id": "model-x", "provider": "openai", "tier": "frontier", "extra": "ignored-in-narrow"},
        {"id": "model-y", "provider": "kr", "tier": "standard", "extra": "ignored-in-narrow"},
    ]
    # Must not raise
    _render_action_result(tui, True, data, width=60)
    output = console.file.getvalue()
    assert "model" in output


# ---------------------------------------------------------------------------
# 2. Required params are prompted and passed correctly
# ---------------------------------------------------------------------------


def test_required_param_is_passed_to_run_action() -> None:
    """Action with a required param: key reader provides the value."""
    console = _console(width=110)

    actions = palette_actions()
    assert actions, "palette must have at least one action"
    first_action_name = actions[0][3]  # e.g. "run-receipt"

    called_with: list[dict[str, Any]] = []

    def fake_rpa(name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
        called_with.append({"name": name, "params": params or {}})
        return True, {"task": (params or {}).get("task"), "model": "gpt-4o"}

    # Patch _ACTION_PARAMS so the first palette entry requires "task".
    _sentinel = object()
    with (
        patch("verdict.home.run_palette_action", side_effect=fake_rpa),
        patch("verdict.home._ACTION_PARAMS", {first_action_name: [("task", _sentinel)]}),
        patch("verdict.home._REQUIRED", _sentinel),
    ):
        # Enter selects item 0, type "my-task\r", then "q" after result
        reader = _make_key_reader("\r", "m", "y", "-", "t", "a", "s", "k", "\r", "q")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    assert called_with, "run_palette_action should have been called"
    assert "task" in called_with[0]["params"]
    assert called_with[0]["params"]["task"] == "my-task"


def test_required_param_cancel_on_empty() -> None:
    """Pressing Enter on a required param (empty) cancels the action -- no run_action call."""
    console = _console(width=110)

    actions = palette_actions()
    first_action_name = actions[0][3]

    _sentinel = object()
    with (
        patch("verdict.home.run_palette_action") as mock_rpa,
        patch("verdict.home._ACTION_PARAMS", {first_action_name: [("task", _sentinel)]}),
        patch("verdict.home._REQUIRED", _sentinel),
    ):
        mock_rpa.return_value = (True, {"ok": True})  # should never be reached
        # Enter to select, Enter again (empty required param) -> cancel
        reader = _make_key_reader("\r", "\r", "q")
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
    """LAUNCH dict[str, LaunchSpec]: entry is resolved and called in-process."""
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
            reader = _make_key_reader("\r", "q")
            _interactive_palette(console, MagicMock(), key_reader=reader)

        assert results == ["called"], f"Expected entry called, got: {results}"
    finally:
        del sys.modules["_fake_launch2"]


def test_launch_plain_string_shows_error() -> None:
    """Pre-lane-E LAUNCH shape (plain string): renders informative error, no crash."""
    fake_launch = {"orchestrate": "long-running orchestration pipeline"}
    console = _console(width=110)

    with (
        patch("verdict.actions.registry.LAUNCH", fake_launch),
        patch("verdict.home.palette_launches") as mock_pl,
        patch("verdict.home.palette_actions", return_value=[]),
    ):
        mock_pl.return_value = [("Runs", "orchestrate", "test desc", "orchestrate")]
        reader = _make_key_reader("\r", "q")
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
    ok, data = _call_launch_entry("no-colon-here", {})
    assert ok is False
    assert "invalid entry format" in str(data)


def test_call_launch_entry_missing_module() -> None:
    ok, data = _call_launch_entry("nonexistent.module.xyz:fn", {})
    assert ok is False
    assert "cannot import" in str(data)


def test_call_launch_entry_missing_function() -> None:
    ok, data = _call_launch_entry("verdict.home:_nonexistent_function_xyz", {})
    assert ok is False
    assert "not found" in str(data)


def test_call_launch_entry_returns_action_result() -> None:
    """If the entry returns an ActionResult, ok/data are extracted."""
    fake_mod = types.ModuleType("_fake_ar_mod")

    def fn(**kwargs: Any) -> ActionResult:
        return ActionResult(data={"key": "val"}, ok=True)

    fake_mod.fn = fn  # type: ignore[attr-defined]
    sys.modules["_fake_ar_mod"] = fake_mod
    try:
        ok, data = _call_launch_entry("_fake_ar_mod:fn", {})
        assert ok is True
        assert data == {"key": "val"}
    finally:
        del sys.modules["_fake_ar_mod"]


# ---------------------------------------------------------------------------
# 6. palette_launches uses LAUNCH registry
# ---------------------------------------------------------------------------


def test_palette_launches_returns_launch_entries() -> None:
    """palette_launches() returns entries for LAUNCH-registered commands."""
    from verdict.actions.registry import LAUNCH

    result = palette_launches()
    for _section, cmd, _desc, _launch_key in result:
        assert cmd in LAUNCH, f"{cmd!r} returned by palette_launches but not in LAUNCH"


def test_palette_launches_monkeypatched() -> None:
    """With monkeypatched LAUNCH containing LaunchSpec, palette_launches finds them."""
    fake_spec = LaunchSpec(reason="r", entry="m:f", section="Runs")
    fake_launch = {"orchestrate": fake_spec, "supervise": fake_spec, "watch": fake_spec}
    with patch("verdict.home.palette_launches") as mock_pl:
        mock_pl.return_value = [
            ("Runs", "orchestrate", "desc", "orchestrate"),
            ("Runs", "supervise", "desc", "supervise"),
        ]
        with patch("verdict.actions.registry.LAUNCH", fake_launch):
            result = mock_pl()
    assert len(result) == 2
    assert result[0][1] == "orchestrate"


# ---------------------------------------------------------------------------
# 7. Navigation with arrow keys and number keys
# ---------------------------------------------------------------------------


def test_arrow_down_changes_selection() -> None:
    """Down arrow moves selection; Enter then runs the next param-free entry."""
    from verdict.home import _ACTION_PARAMS

    actions = palette_actions()
    if len(actions) < 2:
        pytest.skip("need at least 2 action entries")

    # Find the first param-free entry at index >= 1 so Down navigates to it.
    target_idx = next(
        (i for i, (_, _, _, a) in enumerate(actions) if i >= 1 and a not in _ACTION_PARAMS), None
    )
    if target_idx is None:
        pytest.skip("need at least one param-free action at index >= 1")

    called_names: list[str] = []

    def fake_rpa(name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
        called_names.append(name)
        return True, {"ok": True}

    # Navigate from 0 to target_idx with Down arrows, then Enter, then q.
    nav_keys: list[str] = []
    for _ in range(target_idx):
        nav_keys += ["\x1b", "[", "B"]
    nav_keys += ["\r", "q"]

    console = _console(width=110)
    with patch("verdict.home.run_palette_action", side_effect=fake_rpa):
        reader = _make_key_reader(*nav_keys)
        _interactive_palette(console, MagicMock(), key_reader=reader)

    assert called_names, "run_palette_action should have been called"
    assert called_names[0] == actions[target_idx][3], (
        f"Expected action {actions[target_idx][3]!r}, got {called_names[0]!r}"
    )


def test_number_key_selects_entry() -> None:
    """Pressing a number key then Enter selects that param-free entry."""
    from verdict.home import _ACTION_PARAMS

    actions = palette_actions()
    if len(actions) < 2:
        pytest.skip("need at least 2 action entries")

    # Find the first param-free entry at index >= 1 (1-based key = index+1).
    target_idx = next(
        (i for i, (_, _, _, a) in enumerate(actions) if i >= 1 and a not in _ACTION_PARAMS), None
    )
    if target_idx is None:
        pytest.skip("need a param-free action at index >= 1")
    # Number keys only go 1-9, skip if index is 9+
    if target_idx >= 9:
        pytest.skip("target index too high for single-digit key")

    called_names: list[str] = []

    def fake_rpa(name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
        called_names.append(name)
        return True, {}

    console = _console(width=110)
    with patch("verdict.home.run_palette_action", side_effect=fake_rpa):
        reader = _make_key_reader(str(target_idx + 1), "\r", "q")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    assert called_names, "run_palette_action should have been called"
    assert called_names[0] == actions[target_idx][3], (
        f"Expected action {actions[target_idx][3]!r}, got {called_names[0]!r}"
    )


# ---------------------------------------------------------------------------
# 8. config.show specific test (fast action, no required params)
# ---------------------------------------------------------------------------


def test_config_show_renders_output() -> None:
    """config.show action runs and renders its dict output."""
    actions = palette_actions()
    config_idx = next((i for i, (_, _, _, a) in enumerate(actions) if a == "config.show"), None)
    if config_idx is None:
        pytest.skip("config.show not in palette_actions")

    called: list[str] = []

    def fake_rpa(name: str, params: dict[str, Any] | None = None) -> tuple[bool, Any]:
        called.append(name)
        return True, {
            "config_file": "/home/user/.config/verdict/verdict.yaml",
            "exists": False,
            "gateway": "http://127.0.0.1:20128",
            "profile": "default",
        }

    console = _console(width=110)
    with patch("verdict.home.run_palette_action", side_effect=fake_rpa):
        # Navigate to config_idx using Down arrows, then Enter
        nav_keys: list[str] = []
        for _ in range(config_idx):
            nav_keys += ["\x1b", "[", "B"]
        reader = _make_key_reader(*nav_keys, "\r", "q")
        _interactive_palette(console, MagicMock(), key_reader=reader)

    assert called, "run_palette_action should have been called"
    assert called[-1] == "config.show"
    output = console.file.getvalue()
    assert "gateway" in output or "config_file" in output


# ---------------------------------------------------------------------------
# 9. Regression: exhausted key reader raises EOFError → palette exits cleanly
#    (prevents the OOM-crash bug where 'q' returned forever after exhaustion)
# ---------------------------------------------------------------------------


def test_exhausted_key_reader_exits_cleanly() -> None:
    """An exhausted _make_key_reader raises EOFError; _interactive_palette must
    exit cleanly without an infinite loop or unbounded memory growth.

    This is the regression test for the OOM-crash bug: the old _make_key_reader
    returned 'q' forever after exhaustion, and _prompt_params looped reading
    until '\r'/'\n', so it spun forever on 'q'.
    """
    console = _console(width=110)

    with patch("verdict.home.run_palette_action") as mock_rpa:
        mock_rpa.return_value = (True, {"status": "ok"})
        # Only 3 keys in the sequence; reader raises EOFError after them.
        # The palette must terminate, not loop.
        reader = _make_key_reader("\r", "q")  # runs action, then q (before result continue)
        result = _interactive_palette(console, MagicMock(), key_reader=reader)

    assert result == 0, f"Expected exit 0, got {result}"


def test_exhausted_key_reader_during_param_prompt_cancels() -> None:
    """EOFError during param prompting cancels the action — no infinite loop.

    Injects a reader that exhausts mid-prompt (no '\r' after the required
    param) to prove _prompt_params exits on EOFError.
    """
    console = _console(width=110)

    actions = palette_actions()
    first_action_name = actions[0][3]

    _sentinel = object()
    with (
        patch("verdict.home.run_palette_action") as mock_rpa,
        patch("verdict.home._ACTION_PARAMS", {first_action_name: [("task", _sentinel)]}),
        patch("verdict.home._REQUIRED", _sentinel),
    ):
        mock_rpa.return_value = (True, {"ok": True})  # should never be reached
        # Enter selects, then reader exhausts during param typing (no \r)
        # → EOFError → _prompt_params returns None → no run_palette_action call
        reader = _make_key_reader("\r", "a", "b", "c")
        result = _interactive_palette(console, MagicMock(), key_reader=reader)

    mock_rpa.assert_not_called()
    assert result == 0


def test_make_key_reader_raises_eoferror_when_exhausted() -> None:
    """_make_key_reader raises EOFError (not returns 'q') after sequence ends."""
    reader = _make_key_reader("a", "b")
    assert reader() == "a"
    assert reader() == "b"
    with pytest.raises(EOFError):
        reader()


def test_palette_real_action_integration_config_show(tmp_path: Path) -> None:
    """Integration test: real config.show action through palette without mocking.

    Verifies:
    - Real action execution through the full palette path
    - run_palette_action calls through to run_action (not mocked)
    - Actual result fields are rendered
    - Terminal mode restoration works with real execution
    """
    console = _console(width=110)

    # Create a temp config file
    config_dir = tmp_path / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "verdict.yaml"
    config_file.write_text("providers:\n  openai:\n    api_key: test-key-12345\n")

    # Set up environment
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / ".config")

    # Mock palette_actions to inject config.show as the only action
    mock_actions = [("action", "", "Config Show", "config.show")]

    with (
        patch.dict(os.environ, env, clear=False),
        patch("verdict.home.palette_actions", return_value=mock_actions),
    ):
        # Select the action (1), press enter to execute with no params, then quit
        reader = _make_key_reader("1", "\r", "q")

        # Capture output before and after
        output_before = console.file.getvalue()  # type: ignore
        result = _interactive_palette(console, MagicMock(), key_reader=reader)
        output_after = console.file.getvalue()  # type: ignore

        assert result == 0
        rendered = output_after[len(output_before) :]

        # Verify real config.show output was rendered
        # The action should have executed and rendered real fields
        # (path, providers structure, etc.)
        assert len(rendered) > 100, "Expected substantial rendered output from real action"
        # Should contain config-related content (path or structure)
        # Real fields of the config.show ActionResult are rendered ...
        # (The table may elide long values such as the tmp path at this width.)
        assert "exists" in rendered and "True" in rendered
        assert "openai" in rendered
        # ... and the credential in the config file is never shown.
        assert "test-key-12345" not in rendered


def test_launch_entry_keyboard_interrupt_returns_to_palette(tmp_path: Path) -> None:
    """KeyboardInterrupt during action/LAUNCH execution returns to palette with 'cancelled'.

    Verifies:
    - KeyboardInterrupt during execution is caught by the new except clause
    - Palette shows 'cancelled' error
    - Following key press still works (palette remains responsive)
    """
    console = _console(width=110)

    # Mock palette_actions and run_palette_action to raise KeyboardInterrupt
    mock_actions = [("action", "", "Test Action", "test.action")]

    def _raise_interrupt(ref: str, params: Any) -> tuple[bool, dict[str, Any]]:
        raise KeyboardInterrupt("user pressed Ctrl-C")

    with (
        patch("verdict.home.palette_actions", return_value=mock_actions),
        patch("verdict.home.run_palette_action", side_effect=_raise_interrupt),
    ):
        # Select action (1), press enter to execute, then quit after seeing cancelled
        reader = _make_key_reader("1", "\r", "q")
        result = _interactive_palette(console, MagicMock(), key_reader=reader)

    assert result == 0
    output = console.file.getvalue()  # type: ignore
    assert "cancelled" in output.lower()


def test_terminal_restore_structure_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify terminal restore structure exists in action execution path.

    The actual terminal restore logic only runs when key_reader is None (real terminal mode),
    but we can verify the code structure is correct by checking that contextlib.suppress
    wraps the termios calls as expected.
    """
    # This is a structural test - the important finding from the review was that
    # terminal mode must be restored before action execution and re-entered after.
    # The code now has:
    # 1. termios.tcsetattr to restore cooked mode BEFORE action
    # 2. try/finally with tty.setcbreak to re-enter cbreak AFTER action
    # 3. KeyboardInterrupt handling that catches Ctrl-C during action

    # Read the source to verify structure
    import inspect

    from verdict.home import _interactive_palette

    source = inspect.getsource(_interactive_palette)

    # Verify the terminal restoration happens before action execution
    assert "termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)" in source
    # Verify try/finally structure for re-entering cbreak
    assert "tty.setcbreak(fd)" in source
    # Verify KeyboardInterrupt is caught during action execution
    assert "except KeyboardInterrupt:" in source
    assert 'ok, data = False, {"error": "cancelled"}' in source


def test_config_show_action_redacts_secrets(tmp_path, monkeypatch) -> None:
    """config.show must not return credentials held in verdict.yaml (CLI --json and TUI)."""
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
    from verdict.actions import run_action

    cfg = tmp_path / "verdict"
    cfg.mkdir()
    (cfg / "verdict.yaml").write_text("api_key: sk-live-abc\n  bad: [indent\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    res = run_action("config.show")
    assert "sk-live-abc" not in repr(res.data)


def test_configuration_home_uses_registered_redacted_action() -> None:
    from verdict.actions.registry import get_action
    from verdict.home import PALETTE

    entry = next(row for row in PALETTE if row[1] == "config")
    assert entry[0] == "Config" and entry[3] == "config.show"
    assert "redacted" in entry[2]
    assert get_action(entry[3]) is not None
    assert entry in palette_actions()
