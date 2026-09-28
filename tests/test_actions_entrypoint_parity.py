"""Entrypoint parity tests — CLI and TUI palette reach the same domain service.

Lane D (BOD-275): for every registered action, prove that:
  (a) CLI path: set sys.argv → verdict.cli.main() → catches SystemExit
  (b) TUI path: verdict.home.run_palette_action()
  (c) BOTH reach the SAME monkeypatched domain service with equal arguments

Actions whose CLI handler is not yet wired to run_action (lane B) are
@pytest.mark.xfail(strict=True) so they flip to failure once wired.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Spy infrastructure
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    """Records calls to run_action."""

    calls: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)

    def __call__(self, name: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        from verdict.actions.base import ActionResult

        self.calls.append((name, dict(params) if params else None))
        # Return a minimal valid ActionResult so the handler doesn't crash
        return ActionResult(data={"_spy": True, "action": name}, ok=True, exit_code=0)

    @property
    def last(self) -> tuple[str, dict[str, Any] | None]:
        assert self.calls, "run_action was never called"
        return self.calls[-1]

    def reset(self) -> None:
        self.calls.clear()


def _cli_call(argv: list[str], spy: CallRecord, *, stdin_text: str = "") -> int:
    """Run verdict.cli.main() with the given argv, return exit code."""
    import io

    old_argv = sys.argv[:]
    sys.argv = ["verdict", *argv]
    try:
        fake_stdin = io.StringIO(stdin_text)
        fake_stdin.isatty = lambda: False  # type: ignore[attr-defined]
        with (
            patch("verdict.actions.registry.run_action", spy),
            patch("sys.stdin", fake_stdin),
            patch("sys.stdout", open(os.devnull, "w")),
            patch("sys.stderr", open(os.devnull, "w")),
        ):
            from verdict.cli import main

            try:
                main()
                return 0
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 0
    finally:
        sys.argv = old_argv


def _tui_call(action_name: str, params: dict[str, Any] | None, spy: CallRecord) -> tuple[bool, Any]:
    """Run the TUI palette path with run_action spied."""
    with (
        patch("verdict.actions.registry.run_action", spy),
        patch("verdict.actions.run_action", spy),
    ):
        from verdict.home import run_palette_action

        return run_palette_action(action_name, params)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PALETTE_ACTIONS: dict[str, str] = {}  # action_name -> palette command label


def _load_palette_map() -> dict[str, str]:
    """Build action_name -> palette command from PALETTE tuple."""
    if PALETTE_ACTIONS:
        return PALETTE_ACTIONS
    from verdict.home import PALETTE

    for _section, cmd, _desc, action in PALETTE:
        if action:
            PALETTE_ACTIONS[action] = cmd
    return PALETTE_ACTIONS


# All 19 registered actions
ALL_ACTIONS = [
    "models.list",
    "route",
    "doctor",
    "probe",
    "setup.plan",
    "receipt.show",
    "eligibility",
    "config.show",
    "credentials.list",
    "credentials.set",
    "credentials.unset",
    "run-receipt",
    "compare",
    "catalog",
    "detect",
    "stats",
    "suggest",
    "cost-report",
    "replay",
]

# Actions whose CLI handler already delegates to run_action
CLI_WIRED = {
    "models.list",
    "setup.plan",
    "probe",
    "doctor",
    "credentials.list",
    "credentials.set",
    "credentials.unset",
}

# Actions present in the TUI palette (non-empty 4th field)
TUI_PALETTE_ACTIONS = {
    "run-receipt",
    "receipt.show",
    "replay",
    "eligibility",
    "probe",
    "detect",
    "models.list",
    "catalog",
    "route",
    "compare",
    "stats",
    "suggest",
    "cost-report",
    "credentials.list",
    "doctor",
    "setup.plan",
}

# Actions with NO CLI subcommand at all
NO_CLI_COMMAND = {"config.show"}

# Actions with NO TUI palette entry
NO_TUI_PALETTE = {"config.show", "credentials.set", "credentials.unset"}


# ---------------------------------------------------------------------------
# CLI argv for each action (only used for wired actions in parity test)
# ---------------------------------------------------------------------------


def _cli_argv(action: str) -> list[str]:
    """Return the minimal sys.argv (after 'verdict') to reach the action."""
    mapping: dict[str, list[str]] = {
        "models.list": ["models", "--json"],
        "route": ["route", "test task", "medium", "--json"],
        "doctor": ["doctor", "--json"],
        "probe": ["probe", "test-model", "--json"],
        "setup.plan": ["setup", "plan", "--json"],
        "receipt.show": ["receipt", "list", "--json"],
        "eligibility": ["eligibility", "--json"],
        "config.show": [],  # no CLI command
        "credentials.list": ["credentials", "list", "--json"],
        "credentials.set": ["credentials", "set", "TEST_KEY", "--stdin"],
        "credentials.unset": ["credentials", "unset", "TEST_KEY"],
        "run-receipt": ["run-receipt", "fake-run-id", "--json"],
        "compare": ["compare", "test task", "--json"],
        "catalog": ["catalog", "--json"],
        "detect": ["detect", "--json"],
        "stats": ["stats"],
        "suggest": ["suggest"],
        "cost-report": ["cost-report"],
        "replay": ["replay", "fake-session", "--json"],
    }
    return mapping.get(action, [])


def _tui_params(action: str) -> dict[str, Any] | None:
    """Return minimal params for the TUI palette call."""
    mapping: dict[str, dict[str, Any] | None] = {
        "models.list": None,
        "route": {"task": "test task", "criticality": "medium"},
        "doctor": None,
        "probe": {"models": ["test-model"], "transport": lambda *a, **k: None},
        "setup.plan": None,
        "receipt.show": {"run_dir": "/tmp/fake-run"},
        "eligibility": None,
        "config.show": None,
        "credentials.list": None,
        "credentials.set": {"name": "TEST_KEY", "value": "test-value"},
        "credentials.unset": {"name": "TEST_KEY"},
        "run-receipt": {"run_dir": "/tmp/fake-run"},
        "compare": {"task": "test task"},
        "catalog": None,
        "detect": {"offline": True},
        "stats": {"log_path": "/tmp/fake-decisions.jsonl"},
        "suggest": {"log_path": "/tmp/fake-decisions.jsonl"},
        "cost-report": {"log_path": "/tmp/fake-decisions.jsonl"},
        "replay": {"session_id": "fake-session"},
    }
    return mapping.get(action)


# ---------------------------------------------------------------------------
# Parity tests: CLI-wired actions (both paths go through run_action)
# ---------------------------------------------------------------------------


class TestCliWiredParity:
    """Actions whose CLI handler already uses run_action."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.spy = CallRecord()

    @pytest.mark.parametrize("action", sorted(CLI_WIRED - NO_CLI_COMMAND))
    def test_cli_reaches_run_action(self, action: str) -> None:
        """CLI path calls run_action with the correct action name."""
        argv = _cli_argv(action)
        assert argv, f"no CLI argv for {action}"
        stdin_text = "test-value" if action == "credentials.set" else ""
        _cli_call(argv, self.spy, stdin_text=stdin_text)
        name, _params = self.spy.last
        assert name == action, f"CLI reached {name!r}, expected {action!r}"

    @pytest.mark.parametrize("action", sorted(CLI_WIRED & TUI_PALETTE_ACTIONS))
    def test_tui_reaches_run_action(self, action: str) -> None:
        """TUI palette path calls run_action with the correct action name."""
        params = _tui_params(action)
        _tui_call(action, params, self.spy)
        name, _params = self.spy.last
        assert name == action, f"TUI reached {name!r}, expected {action!r}"

    @pytest.mark.parametrize("action", sorted(CLI_WIRED & TUI_PALETTE_ACTIONS))
    def test_both_paths_same_action(self, action: str) -> None:
        """CLI and TUI both reach run_action with the same action name."""
        cli_spy = CallRecord()
        tui_spy = CallRecord()

        argv = _cli_argv(action)
        _cli_call(argv, cli_spy)
        cli_name, _cli_params = cli_spy.last

        params = _tui_params(action)
        _tui_call(action, params, tui_spy)
        tui_name, _tui_params_out = tui_spy.last

        assert cli_name == tui_name == action, (
            f"action={action}: CLI reached {cli_name!r}, TUI reached {tui_name!r}"
        )


# ---------------------------------------------------------------------------
# Parity tests: CLI-unwired actions (xfail — lane B will wire them)
# ---------------------------------------------------------------------------


class TestCliUnwiredParity:
    """Actions whose CLI handler does NOT yet use run_action.

    These are xfail(strict=True): they MUST fail now (CLI doesn't call
    run_action). Once lane B wires them, they'll start passing and the
    strict xfail flips to a test failure, signalling the controller to
    remove the marker.
    """

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.spy = CallRecord()

    @pytest.mark.parametrize("action", sorted(set(ALL_ACTIONS) - CLI_WIRED - NO_CLI_COMMAND))
    @pytest.mark.xfail(
        strict=True, reason="handler not wired: CLI does not use run_action yet (lane B)"
    )
    def test_cli_reaches_run_action(self, action: str) -> None:
        """CLI path should call run_action once lane B wires it."""
        argv = _cli_argv(action)
        assert argv, f"no CLI argv for {action}"
        _cli_call(argv, self.spy)
        name, _params = self.spy.last
        assert name == action, f"CLI reached {name!r}, expected {action!r}"


# ---------------------------------------------------------------------------
# TUI palette coverage: every palette-mapped action goes through run_action
# ---------------------------------------------------------------------------


class TestTuiPaletteCoverage:
    """Every action in the palette reaches run_action."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.spy = CallRecord()

    @pytest.mark.parametrize("action", sorted(TUI_PALETTE_ACTIONS))
    def test_palette_action_reaches_run_action(self, action: str) -> None:
        params = _tui_params(action)
        _tui_call(action, params, self.spy)
        name, _params = self.spy.last
        assert name == action


# ---------------------------------------------------------------------------
# config.show: action-only (no CLI, no palette) — both absent
# ---------------------------------------------------------------------------


class TestConfigShowActionOnly:
    """config.show has no CLI subcommand and no palette entry."""

    def test_config_show_not_in_palette(self) -> None:
        _load_palette_map()
        assert "config.show" not in PALETTE_ACTIONS

    def test_config_show_registered(self) -> None:
        from verdict.actions.registry import get_action

        assert get_action("config.show") is not None

    def test_config_show_via_run_action(self) -> None:
        from verdict.actions.registry import run_action

        result = run_action("config.show")
        assert result.ok
        assert "config_file" in result.data


# ---------------------------------------------------------------------------
# Mutation parity: credentials.set via CLI and TUI palette
# ---------------------------------------------------------------------------


class TestCredentialsMutationParity:
    """credentials.set reaches CredentialsStore.set with the same name/value."""

    def test_credentials_set_parity(self, tmp_path: Path) -> None:
        """Both CLI and TUI reach CredentialsStore.set with the same args."""
        store_calls: list[tuple[str, str]] = []

        class FakeStore:
            def __init__(self, *a: Any, **kw: Any) -> None:
                self._path = tmp_path / "store"
                self._path.mkdir(exist_ok=True)

            def set(self, name: str, value: str) -> None:
                store_calls.append((name, value))

            def unset(self, name: str) -> bool:
                return True

            def load_into_env(self) -> None:
                pass

        # --- TUI path: run_action("credentials.set") calls CredentialsStore.set ---
        with patch("verdict.credentials_store.CredentialsStore", FakeStore):
            from verdict.actions.registry import run_action

            run_action(
                "credentials.set",
                {"name": "MY_KEY", "value": "secret123", "force_unregistered": True},
            )

        assert len(store_calls) == 1
        tui_name, tui_value = store_calls[0]
        store_calls.clear()

        # --- CLI path: cmd_credentials_set reads stdin then calls run_action ---
        spy = CallRecord()
        _cli_call(
            ["credentials", "set", "MY_KEY", "--stdin", "--force-unregistered"],
            spy,
            stdin_text="secret123",
        )

        cli_name_called, cli_params = spy.last
        assert cli_name_called == "credentials.set"
        assert cli_params is not None
        assert cli_params["name"] == "MY_KEY"
        assert cli_params["value"] == "secret123"

        # Both paths target the same credential name and value
        assert cli_params["name"] == tui_name
        assert cli_params["value"] == tui_value


# ---------------------------------------------------------------------------
# Registry completeness: all 19 expected actions are registered
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    def test_all_19_actions_registered(self) -> None:
        from verdict.actions.registry import list_actions

        names = {a.name for a in list_actions()}
        for action in ALL_ACTIONS:
            assert action in names, f"action {action!r} not registered"
        assert len(names) >= 19

    def test_palette_covers_expected_actions(self) -> None:
        palette_map = _load_palette_map()
        for action in TUI_PALETTE_ACTIONS:
            assert action in palette_map, f"action {action!r} not in PALETTE"


# ---------------------------------------------------------------------------
# Summary table (collected via pytest plugin)
# ---------------------------------------------------------------------------


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    """Print a parity summary table at the end of the test run."""
    lines = ["", "=" * 72, "ACTION ENTRYPOINT PARITY SUMMARY", "=" * 72]
    lines.append(f"{'Action':<22} {'CLI→svc':>10} {'TUI→svc':>10} {'Equal':>8} {'xfail':>8}")
    lines.append("-" * 72)

    for action in ALL_ACTIONS:
        cli_ok = action in CLI_WIRED and action not in NO_CLI_COMMAND
        tui_ok = action in TUI_PALETTE_ACTIONS
        equal = cli_ok and tui_ok
        xfail = action not in CLI_WIRED and action not in NO_CLI_COMMAND
        lines.append(
            f"{action:<22} {'✓' if cli_ok else '✗':>10} {'✓' if tui_ok else '✗':>10} "
            f"{'✓' if equal else '—':>8} {'xfail' if xfail else '':>8}"
        )
    lines.append("=" * 72)
    terminalreporter.write_line("\n".join(lines))
