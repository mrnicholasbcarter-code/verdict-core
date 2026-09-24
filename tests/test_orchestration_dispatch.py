"""Every orchestration subcommand must reach its handler through `verdict` (not fall back to help)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "verdict", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_run_receipt_dispatches_and_verifies(tmp_path: Path) -> None:
    run = tmp_path / "r1"
    run.mkdir()
    (run / "events.jsonl").write_text("")
    proc = _run("run-receipt", str(run), cwd=tmp_path)
    assert "usage:" not in proc.stdout.splitlines()[0] if proc.stdout else True
    assert proc.returncode == 2 and "no receipt" in proc.stderr


def test_watch_dispatches(tmp_path: Path) -> None:
    proc = _run("watch", str(tmp_path / "missing"), "--once", cwd=tmp_path)
    assert proc.returncode == 2 and "no run" in proc.stderr


def test_orchestrate_dispatches_and_fails_closed_without_key(tmp_path: Path) -> None:
    env_free = subprocess.run(
        [sys.executable, "-m", "verdict", "orchestrate", "goal", "--repo", str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert env_free.returncode == 2 and "VERDICT_OMNIROUTE_API_KEY" in env_free.stderr


def test_supervise_and_eligibility_are_dispatched(tmp_path: Path) -> None:
    help_sup = _run("supervise", "--help", cwd=tmp_path)
    assert "--run-id" in help_sup.stdout
    elig = _run("eligibility", "--help", cwd=tmp_path)
    assert "--probe" in elig.stdout
    assert json  # keep import used
