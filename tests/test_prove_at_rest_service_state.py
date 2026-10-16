"""Shared timer-state check reused by doctor and setup (BOD-411)."""

from __future__ import annotations

import subprocess

from verdict.prove_at_rest_service import timer_state


def _fake_run(stdout: str):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def test_timer_state_missing_when_unit_not_loaded() -> None:
    run = _fake_run("LoadState=not-found\nActiveState=inactive\nSubState=dead\n")
    state = timer_state("verdict-prove-at-rest.timer", run=run)
    assert state == {"installed": False, "failed": False, "active": False, "state": "missing"}


def test_timer_state_failed_when_active_state_failed() -> None:
    run = _fake_run("LoadState=loaded\nActiveState=failed\nSubState=failed\n")
    state = timer_state("verdict-prove-at-rest.timer", run=run)
    assert state == {"installed": True, "failed": True, "active": False, "state": "failed"}


def test_timer_state_active_when_running() -> None:
    run = _fake_run("LoadState=loaded\nActiveState=active\nSubState=running\n")
    state = timer_state("verdict-prove-at-rest.timer", run=run)
    assert state == {"installed": True, "failed": False, "active": True, "state": "active"}


def test_timer_state_inactive_when_loaded_but_not_running() -> None:
    run = _fake_run("LoadState=loaded\nActiveState=inactive\nSubState=dead\n")
    state = timer_state("verdict-prove-at-rest.timer", run=run)
    assert state == {"installed": True, "failed": False, "active": False, "state": "inactive"}


def test_timer_state_never_raises_on_systemctl_error() -> None:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        raise OSError("systemctl not found")

    state = timer_state("verdict-prove-at-rest.timer", run=run)
    assert state["state"] == "missing"
