"""What the CLI prints must not mislead: receipt integrity, pure --json, and --plain.

Each test names the defect it catches (T2 workflow gaps G5, G6 and issue #1138).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any, ClassVar

import pytest

import verdict.actions.helpers as _action_helpers
from verdict import cli
from verdict.models import RoutingDecision

COMPLETE_RUN = Path(__file__).parent / "fixtures" / "metrics" / "complete-run"


def _receipt_args(run_dir: Path, *, as_json: bool) -> argparse.Namespace:
    return argparse.Namespace(run=str(run_dir), runs_dir=str(run_dir.parent), json=as_json)


def _copy_run(tmp_path: Path, *, tamper: bool) -> Path:
    run_dir = tmp_path / "run"
    shutil.copytree(COMPLETE_RUN, run_dir)
    if tamper:
        events = run_dir / "events.jsonl"
        events.write_text(events.read_text().replace("free/model-a", "free/model-z", 1))
    return run_dir


# --- G5: a tampered receipt must not lead with the recorded success -------------------


def test_tampered_receipt_first_line_reports_failed_verification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: first line 'COMPLETE ... review PASS' on a receipt that failed integrity."""
    from verdict.orchestration.cli import _receipt

    rc = _receipt(_receipt_args(_copy_run(tmp_path, tamper=True), as_json=False))
    lines = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert lines[0].startswith("INTEGRITY FAILED:")
    assert "events_digest mismatch" in lines[0]
    assert "COMPLETE" not in lines[0]
    assert any(line.startswith("recorded (unverified): COMPLETE") for line in lines[1:])


def test_tampered_receipt_json_is_not_a_bare_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: --json 'outcome': 'COMPLETE' for a receipt whose events digest is wrong."""
    from verdict.orchestration.cli import _receipt

    rc = _receipt(_receipt_args(_copy_run(tmp_path, tamper=True), as_json=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["verified"] is False
    assert payload["outcome"] == "INTEGRITY_FAILED"
    assert payload["recorded_outcome"] == "COMPLETE"
    assert any("events_digest mismatch" in p for p in payload["problems"])


def test_untampered_receipt_output_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: the integrity fix changing output for a receipt that verifies."""
    from verdict.orchestration.cli import _receipt

    run_dir = _copy_run(tmp_path, tamper=False)
    assert _receipt(_receipt_args(run_dir, as_json=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("COMPLETE: ")
    assert lines[1] == "integrity: OK (events digest verified)"
    assert not any("unverified" in line for line in lines)

    assert _receipt(_receipt_args(run_dir, as_json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True and payload["outcome"] == "COMPLETE"


# --- G6: route --json prints exactly one JSON document on stdout ----------------------


class _NoisyConsole:
    """Stands in for a terminal console: a spinner writes to stdout while it runs."""

    def status(self, *_a: Any, **_k: Any) -> Any:
        print("SPINNER Evaluating static catalog (offline)...")
        import contextlib

        return contextlib.nullcontext()


def _offline_gate(decision: str = "fallback") -> Any:
    class _Gate:
        providers: ClassVar[dict[str, cli.ProviderConfig]] = {}

        def route(self, task: str, criticality: str, context: object = None) -> RoutingDecision:
            del task, criticality, context
            return RoutingDecision(
                model="anthropic/claude-opus-5",
                provider="primary",
                tier=0,
                reason="catalog default",
                decision=decision,
                transport_outcome="not_sent",
            )

        def route_with_strategy(self, task: str, criticality: str, context: object = None) -> Any:
            from verdict.gate import strategy_from_decision

            decision = self.route(task, criticality, context)
            return decision, strategy_from_decision(decision)

    return _Gate()


def test_route_allow_offline_json_stdout_is_one_json_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: human card and spinner text before the JSON, and exit 1 after a decision."""
    monkeypatch.setattr(cli, "console", _NoisyConsole())
    monkeypatch.setattr(
        _action_helpers, "build_route_gate", lambda allow_offline=False: _offline_gate()
    )
    cli.cmd_route("write hello", "medium", output_json=True, allow_offline=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["model"] == "anthropic/claude-opus-5"
    assert payload["decision"] == "fallback"
    assert payload["transport_outcome"] == "error"


def test_route_json_denied_decision_exits_nonzero_with_json_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: a denied (no route) decision reported with exit 0 or non-JSON stdout."""
    monkeypatch.setattr(cli, "console", _NoisyConsole())
    monkeypatch.setattr(
        _action_helpers, "build_route_gate", lambda allow_offline=False: _offline_gate("denied")
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_route("write hello", "medium", output_json=True, allow_offline=True)
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "denied" and payload["error"]


def test_route_json_without_legacy_has_no_spinner_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches: json-output-spinner-leak on the execution-path route --json branch."""
    from verdict.actions import registry
    from verdict.actions.registry import ActionResult

    monkeypatch.setattr(cli, "console", _NoisyConsole())
    selected = {"route_id": "cc/model", "tier": 1, "provider": "cc"}
    monkeypatch.setattr(
        registry,
        "run_action",
        lambda name, params: ActionResult(data={"selected": selected, "reasons": []}),
    )
    cli.cmd_route("write hello", "medium", output_json=True)
    assert json.loads(capsys.readouterr().out)["selected"]["route_id"] == "cc/model"


# --- #1138: orchestrate --plain gives the ASCII narrative even on a pty ---------------


@pytest.fixture
def pty_stream() -> Any:
    master, slave = os.openpty()
    stream = os.fdopen(slave, "w")
    try:
        yield stream
    finally:
        stream.close()
        os.close(master)


@pytest.fixture
def colour_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "NO_COLOR", "VERDICT_PLAIN", "TERM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


@pytest.mark.usefixtures("colour_env")
def test_orchestrate_plain_flag_forces_plain_view_on_pty(pty_stream: Any) -> None:
    """Catches: --plain ignored, so a pty gets the coloured live view."""
    from verdict.orchestration.cli import _follow_console
    from verdict.orchestration.tui import plain_mode

    assert plain_mode(_follow_console(plain=False, stream=pty_stream)) is False
    assert plain_mode(_follow_console(plain=True, stream=pty_stream)) is True


@pytest.mark.usefixtures("colour_env")
def test_verdict_plain_env_forces_plain_view_on_pty(
    pty_stream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches: VERDICT_PLAIN=1 ignored by the orchestrate progress view on a pty."""
    from verdict.orchestration.cli import _follow_console
    from verdict.orchestration.tui import plain_mode

    monkeypatch.setenv("VERDICT_PLAIN", "1")
    assert plain_mode(_follow_console(plain=False, stream=pty_stream)) is True
