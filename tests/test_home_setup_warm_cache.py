"""Setup's warm-cache step reports the real timer state (BOD-411).

Setup must not claim "Service installer: not available yet" when
``verdict prove-at-rest install-service`` has existed since #802: it calls
the installer and reports the same timer state doctor checks.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

import verdict.prove_at_rest_service as prove_at_rest_service
from verdict.home_setup_render import _warm_cache
from verdict.terminal_ui import TerminalUI


@pytest.fixture
def ui() -> TerminalUI:
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    return TerminalUI(console)


def test_warm_cache_choosing_on_calls_install_service_and_reports_real_state(
    ui: TerminalUI, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_calls = []

    def fake_manage_service(**kwargs):
        install_calls.append(kwargs)
        return {"changed": True}

    monkeypatch.setattr(prove_at_rest_service, "manage_service", fake_manage_service)

    states = iter(
        [
            {"state": "inactive", "installed": False, "failed": False, "active": False},
            {"state": "active", "installed": True, "failed": False, "active": True},
        ]
    )
    monkeypatch.setattr(prove_at_rest_service, "timer_state", lambda unit: next(states))

    answers = iter(["on", "y"])
    notes: list[str] = []
    _warm_cache(ui, lambda: next(answers, None), "http://localhost:20128", notes)

    output = ui.console.file.getvalue()
    assert "not available yet" not in output
    assert install_calls, "manage_service must be called when the operator chooses on"
    assert any("active" in note for note in notes)


def test_warm_cache_choosing_off_leaves_timer_untouched(
    ui: TerminalUI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prove_at_rest_service,
        "timer_state",
        lambda unit: {"state": "active", "installed": True, "failed": False, "active": True},
    )
    install_calls = []
    monkeypatch.setattr(
        prove_at_rest_service, "manage_service", lambda **kwargs: install_calls.append(kwargs)
    )

    notes: list[str] = []
    _warm_cache(ui, lambda: "off", "http://localhost:20128", notes)

    assert not install_calls
    assert any("unchanged" in note for note in notes)
