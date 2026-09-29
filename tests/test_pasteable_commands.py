"""The commands Verdict prints must work when a user pastes them.

End-to-end through the real CLI (subprocess), from a clean working directory.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "proof" / "demo-run"
LIVE = ROOT / "docs" / "proof" / "dogfood-bod-225-live-2026-09-29"


def _verdict(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(ROOT), "COLUMNS": "120"}
    return subprocess.run(
        [sys.executable, "-m", "verdict", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def _hints(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# verdict ") and "--panel context" in stripped:
            out.append(stripped[len("# verdict ") :])
    return out


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    shutil.copytree(FIXTURE, runs / "r1")
    shutil.copytree(FIXTURE, tmp_path / "direct-run")
    return tmp_path


@pytest.mark.parametrize(
    "trace_args",
    [["trace", "direct-run"], ["trace", "r1", "--runs-dir", "runs"]],
    ids=["run-dir-path", "run-id-with-runs-dir"],
)
def test_printed_context_hint_runs_when_pasted(workdir: Path, trace_args: list[str]) -> None:
    trace = _verdict(trace_args, workdir)
    assert trace.returncode == 0, trace.stderr
    hints = _hints(trace.stdout)
    assert hints, f"no context hint printed:\n{trace.stdout}"
    for hint in hints:
        pasted = _verdict(shlex.split(hint), workdir)
        assert pasted.returncode == 0, f"`verdict {hint}` failed: {pasted.stderr}"
        assert pasted.stdout.startswith("context"), pasted.stdout[:200]
        node = hint.split("--node", 1)[1].split()[0]
        assert f"node {node}" in pasted.stdout


def test_offline_demo_prints_no_hint_to_its_deleted_run(tmp_path: Path) -> None:
    demo = _verdict(["demo", "--speed", "0"], tmp_path)
    assert demo.returncode == 0, demo.stderr
    assert "--panel context" not in demo.stdout
    assert "OFFLINE SCENARIO" in demo.stdout


def test_missing_run_error_shows_the_path_once(tmp_path: Path) -> None:
    missing = _verdict(["trace", "nope", "--node", "n", "--panel", "context"], tmp_path)
    assert missing.returncode != 0
    message = missing.stderr + missing.stdout
    assert ".verdict/runs/nope" in message
    assert ".verdict/runs/.verdict/runs" not in message


@pytest.mark.parametrize(
    "receipt_args",
    [
        ["--runs-dir", ".", "docs/proof/dogfood-bod-225-live-2026-09-29"],
        ["--runs-dir", "docs/proof", "dogfood-bod-225-live-2026-09-29"],
        ["docs/proof/dogfood-bod-225-live-2026-09-29"],
        [str(LIVE)],
    ],
    ids=["readme-form", "id-with-runs-dir", "relative-path", "absolute-path"],
)
def test_run_receipt_accepts_every_documented_form(receipt_args: list[str]) -> None:
    result = _verdict(["run-receipt", *receipt_args], ROOT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("COMPLETE"), result.stdout[:200]
    assert "integrity: OK" in result.stdout
