"""BOD-187: autodev/packet/resume/benchmark human output goes through verdict.present."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from verdict import cli, present
from verdict.terminal_ui import TerminalUI

GOLDEN_INPUTS = Path(__file__).parent / "fixtures" / "cli_golden" / "inputs"


@pytest.fixture()
def plain(monkeypatch: pytest.MonkeyPatch):
    """Presenter bound to a captured non-terminal console (plain mode)."""
    buf = io.StringIO()
    console = Console(file=buf, width=100, force_terminal=False, color_system=None)
    present.reset(TerminalUI(console))
    yield buf
    present.reset(None)


def test_packet_inspect_human_uses_presentation(plain: io.StringIO, tmp_path: Path) -> None:
    path = tmp_path / "packet.json"
    shutil.copy(GOLDEN_INPUTS / "packet.json", path)
    cli.cmd_autodev_packet("inspect", str(path), output_json=False)
    out = plain.getvalue()
    assert "VERDICT" in out
    assert "Autodev packet" in out
    assert "next safe action" in out
    assert "\x1b[" not in out


def test_packet_inspect_json_untouched_by_presentation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "packet.json"
    shutil.copy(GOLDEN_INPUTS / "packet.json", path)
    cli.cmd_autodev_packet("inspect", str(path), output_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["packet_id"]
    assert payload["proof_level"]


def test_packet_error_human_is_presented_and_exits_one(plain: io.StringIO, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_autodev_packet("inspect", str(tmp_path / "missing.json"), output_json=False)
    assert exc.value.code == 1
    out = plain.getvalue()
    assert "VERDICT" in out
    assert "FAILED" in out


def test_packet_execute_consent_refusal_human(plain: io.StringIO, tmp_path: Path) -> None:
    path = tmp_path / "packet.json"
    shutil.copy(GOLDEN_INPUTS / "packet.json", path)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_autodev_packet_execute(str(path), str(tmp_path), output_json=False)
    assert exc.value.code == 2
    out = plain.getvalue()
    assert "VERDICT" in out
    assert "pass --allow-live" in out


def test_packet_execute_consent_refusal_json_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "packet.json"
    shutil.copy(GOLDEN_INPUTS / "packet.json", path)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_autodev_packet_execute(str(path), str(tmp_path), output_json=True)
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "packet execute calls live routes and edits the working tree; pass --allow-live"
    }


def _decision(task: str):
    from verdict.execution_path import optimize_execution_path
    from verdict.subagent_resolver import public_execution_path_request

    request = {
        "schema_version": "1",
        "trajectory_id": "traj-present-autodev",
        "slice_id": "slice-present-autodev",
        "acceptance_criteria": ["worker launches only on the selected route"],
        "proof_criteria": ["selected and served identities match"],
        "candidates": [
            {
                "strategy": "direct_cheap",
                "route_id": "route-selected-model",
                "gateway": "http://gateway.test/v1",
                "provider": "provider-a",
                "model": "provider/model",
                "capability_tier": 2,
                "eligible": True,
                "is_free": True,
                "execution_tokens": 16,
                "verification_tokens": 4,
                "certification_state": "ready",
                "certification_freshness": "fresh",
            }
        ],
    }
    return optimize_execution_path(public_execution_path_request(request, task=task))


def test_autodev_consent_refusal_human(plain: io.StringIO, tmp_path: Path) -> None:
    decision = _decision("demo task")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_autodev(
            "demo task",
            str(tmp_path),
            orchestrator_model=None,
            executor_model=None,
            base_url=None,
            output_json=False,
            allow_live=False,
            execution_path_decision=decision,
        )
    assert exc.value.code == 2
    out = plain.getvalue()
    assert "VERDICT" in out
    assert "pass --allow-live to consent" in out


def test_resume_failure_human_is_presented(plain: io.StringIO, tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_resume("BOD-0", output_json=False, repo=tmp_path)
    assert exc.value.code == 1
    out = plain.getvalue()
    assert "VERDICT" in out
    assert "Resume" in out
    assert "FAILED" in out


def test_shadow_human_error_is_presented(plain: io.StringIO, tmp_path: Path) -> None:
    episodes = tmp_path / "episodes.json"
    episodes.write_text(json.dumps({"episodes": 42}))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_autodev_packet_shadow(str(episodes), output_json=False)
    assert exc.value.code == 1
    out = plain.getvalue()
    assert "VERDICT" in out
    assert "must be a list" in out


def test_benchmark_report_body_stays_raw_for_piping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = Path(__file__).parent.parent / "benchmarks" / "fixtures" / "reproducible.json"
    cli.cmd_benchmark(str(fixture), str(tmp_path / "report.json"))
    out = capsys.readouterr().out
    assert "mode: local-reproducible" in out
