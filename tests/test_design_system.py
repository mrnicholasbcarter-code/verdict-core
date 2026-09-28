"""Tests for verdict.design — tokens, glyphs, and presentation mode policy."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from verdict.design import GLYPHS, TOKENS, presentation_mode

# ---------------------------------------------------------------------------
# Token contract: existing keys and values must be unchanged
# ---------------------------------------------------------------------------

ORIGINAL_TOKENS = {
    "PRIMARY": "bold #54c7b0",
    "SECONDARY": "#8db8d8",
    "ACCENT": "#e5b567",
    "MUTED": "#929ca6",
    "SUCCESS": "#83c995",
    "WARNING": "#e5b567",
    "ERROR": "bold #ee8585",
    "INFO": "#8db8d8",
    "BORDER": "#647580",
}


def test_existing_token_keys_present() -> None:
    for key in ORIGINAL_TOKENS:
        assert key in TOKENS, f"TOKENS missing key {key!r}"


def test_existing_token_values_unchanged() -> None:
    for key, value in ORIGINAL_TOKENS.items():
        assert TOKENS[key] == value, f"TOKENS[{key!r}] changed: {TOKENS[key]!r} != {value!r}"


def test_new_semantic_keys_present() -> None:
    for key in ("DEGRADED", "COOLDOWN", "DISABLED", "UNKNOWN"):
        assert key in TOKENS, f"TOKENS missing new key {key!r}"


def test_warning_and_accent_are_separate_semantics() -> None:
    # They may share a hue for now, but they must be independently defined keys.
    assert "WARNING" in TOKENS
    assert "ACCENT" in TOKENS
    # Both exist as distinct dictionary entries (even if values coincide today).
    # The test asserts their presence; value equality is acceptable per spec.


# ---------------------------------------------------------------------------
# GLYPHS contract: all state keys present, each has ASCII fallback
# ---------------------------------------------------------------------------

EXPECTED_GLYPH_KEYS = {"running", "validated", "failed", "reassigned", "planned", "blocked"}


def test_glyphs_keys_complete() -> None:
    assert set(GLYPHS.keys()) == EXPECTED_GLYPH_KEYS


def test_glyphs_have_ascii_fallbacks() -> None:
    for key, (rich_g, ascii_g, token) in GLYPHS.items():
        assert len(ascii_g) == 1 and ascii_g.isascii(), (
            f"GLYPHS[{key!r}] ascii_glyph {ascii_g!r} must be a single ASCII char"
        )
        assert rich_g, f"GLYPHS[{key!r}] rich_glyph is empty"
        assert token in TOKENS, f"GLYPHS[{key!r}] token {token!r} not in TOKENS"


# ---------------------------------------------------------------------------
# Helper: build a fake TTY stream
# ---------------------------------------------------------------------------


class _FakeTTY(io.StringIO):
    """StringIO that claims to be a TTY."""

    def isatty(self) -> bool:
        return True


class _FakeNonTTY(io.StringIO):
    def isatty(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# presentation_mode: forced-plain conditions
# ---------------------------------------------------------------------------


def test_no_color_gives_no_color() -> None:
    mode = presentation_mode(_FakeTTY(), env={"NO_COLOR": ""})
    assert mode.color is False
    assert mode.animate is False
    assert mode.unicode is False


def test_ci_gives_no_color() -> None:
    mode = presentation_mode(_FakeTTY(), env={"CI": "true"})
    assert mode.color is False
    assert mode.animate is False


def test_term_dumb_gives_no_color() -> None:
    mode = presentation_mode(_FakeTTY(), env={"TERM": "dumb"})
    assert mode.color is False
    assert mode.animate is False


def test_verdict_plain_forces_plain_on_a_tty() -> None:
    # VERDICT_PLAIN=1 forced plain output in orchestration plain_mode() before this module.
    mode = presentation_mode(_FakeTTY(), env={"VERDICT_PLAIN": "1"})
    assert mode.color is False
    assert mode.animate is False


def test_orchestration_plain_mode_honours_verdict_plain(monkeypatch) -> None:
    from rich.console import Console

    from verdict.orchestration.tui import plain_mode

    monkeypatch.setenv("VERDICT_PLAIN", "1")
    for name in ("NO_COLOR", "CI", "TERM"):
        monkeypatch.delenv(name, raising=False)
    assert plain_mode(Console(force_terminal=True)) is True


def test_orchestration_plain_mode_matches_main_for_forced_terminal(monkeypatch) -> None:
    # A console forced to be a terminal is not plain when no plain signal is set
    # (origin/main behaviour: plain_mode checked console.is_terminal).
    from rich.console import Console

    from verdict.orchestration.tui import plain_mode

    for name in ("NO_COLOR", "CI", "TERM", "VERDICT_PLAIN"):
        monkeypatch.delenv(name, raising=False)
    assert plain_mode(Console(force_terminal=True)) is False
    assert plain_mode(Console(force_terminal=False, file=__import__("io").StringIO())) is True


def test_non_tty_gives_no_color() -> None:
    mode = presentation_mode(_FakeNonTTY(), env={})
    assert mode.color is False
    assert mode.animate is False


def test_verdict_no_animation_disables_animate() -> None:
    mode = presentation_mode(_FakeTTY(), env={"VERDICT_NO_ANIMATION": "1"})
    assert mode.animate is False
    # color should still be on (TTY, no NO_COLOR/CI/dumb)
    assert mode.color is True


# ---------------------------------------------------------------------------
# presentation_mode: width resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("columns,expected", [("40", 40), ("60", 60), ("200", 200)])
def test_columns_env_sets_width(columns: str, expected: int) -> None:
    mode = presentation_mode(_FakeTTY(), env={"COLUMNS": columns})
    assert mode.width == expected


# ---------------------------------------------------------------------------
# presentation_mode: SSH detection
# ---------------------------------------------------------------------------


def test_ssh_connection_allows_animation() -> None:
    mode = presentation_mode(_FakeTTY(), env={"SSH_CONNECTION": "10.0.0.1 12345 10.0.0.2 22"})
    assert mode.animate is True
    assert mode.color is True


# ---------------------------------------------------------------------------
# presentation_mode: tmux detection
# ---------------------------------------------------------------------------


def test_tmux_env_allows_color_and_animation() -> None:
    mode = presentation_mode(_FakeTTY(), env={"TMUX": "/tmp/tmux-1000/default,12345,0"})
    assert mode.color is True
    assert mode.animate is True


# ---------------------------------------------------------------------------
# presentation_mode: no sleep ever called
# ---------------------------------------------------------------------------


def test_presentation_mode_never_calls_sleep() -> None:
    """presentation_mode must not call time.sleep() or any delay primitive."""
    import time as _time_module

    original_sleep = _time_module.sleep
    sleep_called = []

    def _spy_sleep(seconds: float) -> None:
        sleep_called.append(seconds)
        original_sleep(0)  # don't actually wait

    with patch.object(_time_module, "sleep", side_effect=_spy_sleep):
        for env in [
            {},
            {"NO_COLOR": ""},
            {"CI": "true"},
            {"VERDICT_NO_ANIMATION": "1"},
            {"SSH_CONNECTION": "x"},
            {"TMUX": "x"},
        ]:
            presentation_mode(_FakeTTY(), env=env)

    assert sleep_called == [], f"presentation_mode called time.sleep({sleep_called})"


# ---------------------------------------------------------------------------
# Back-compat: terminal_ui still exports TOKENS
# ---------------------------------------------------------------------------


def test_terminal_ui_still_exports_tokens() -> None:
    from verdict.terminal_ui import TOKENS as TUI_TOKENS

    assert TUI_TOKENS is TOKENS or TUI_TOKENS == TOKENS


# ---------------------------------------------------------------------------
# Back-compat: orchestration.tui still exports GLYPHS
# ---------------------------------------------------------------------------


def test_orchestration_tui_still_exports_glyphs() -> None:
    from verdict.orchestration.tui import GLYPHS as TUI_GLYPHS

    assert TUI_GLYPHS is GLYPHS or TUI_GLYPHS == GLYPHS
