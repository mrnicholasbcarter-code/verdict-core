"""Tests for the merge-guard subcommand in scripts/prime_workflow.py."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_SCRIPT = REPO_ROOT / "scripts" / "prime_workflow.py"
PYTHON = sys.executable


def _run_merge_guard(
    state_dir: Path, command: list[str], *, timeout: float | None = None, repo: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the merge-guard subcommand and return the completed process."""
    args = [PYTHON, str(WORKFLOW_SCRIPT), "merge-guard", "--state-dir", str(state_dir)]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    if repo is not None:
        args += ["--repo", str(repo)]
    args += ["--", *command]
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


class TestMergeGuardSerialization:
    """Two concurrent merge-guard runs must not overlap."""

    def test_no_overlap(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        marker_a = tmp_path / "a.json"
        marker_b = tmp_path / "b.json"

        # Each child runs a script that writes start time, sleeps, writes end time
        helper = tmp_path / "helper.py"
        helper.write_text(
            "import json, sys, time\n"
            "out = sys.argv[1]\n"
            "start = time.monotonic()\n"
            "time.sleep(0.3)\n"
            "end = time.monotonic()\n"
            'json.dump({"start": start, "end": end}, open(out, "w"))\n'
        )

        args_base = [
            PYTHON,
            str(WORKFLOW_SCRIPT),
            "merge-guard",
            "--state-dir",
            str(state_dir),
            "--",
            PYTHON,
            str(helper),
        ]

        proc_a = subprocess.Popen(
            [*args_base, str(marker_a)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Small delay so both processes are alive and competing for the lock.
        time.sleep(0.05)
        proc_b = subprocess.Popen(
            [*args_base, str(marker_b)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        proc_a.wait(timeout=15)
        proc_b.wait(timeout=15)

        assert proc_a.returncode == 0
        assert proc_b.returncode == 0

        ts_a: dict[str, float] = json.loads(marker_a.read_text())
        ts_b: dict[str, float] = json.loads(marker_b.read_text())

        # Ranges must not overlap: one must finish before the other starts.
        a_before_b = ts_a["end"] <= ts_b["start"]
        b_before_a = ts_b["end"] <= ts_a["start"]
        assert a_before_b or b_before_a, (
            f"Overlapping runs: A=[{ts_a['start']:.3f}, {ts_a['end']:.3f}] "
            f"B=[{ts_b['start']:.3f}, {ts_b['end']:.3f}]"
        )


class TestMergeGuardTimeout:
    """Lock timeout must exit non-zero without running the command."""

    def test_timeout_no_run(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        marker = tmp_path / "should_not_exist"

        # Hold the lock externally.
        lock_path = state_dir / "integration.lock"
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        try:
            result = _run_merge_guard(
                state_dir, [PYTHON, "-c", f"open({str(marker)!r}, 'w').write('ran')"], timeout=0.2
            )
            assert result.returncode != 0, "Expected non-zero exit on timeout"
            assert not marker.exists(), "Command must NOT run when lock times out"
            assert "failed to acquire" in result.stderr
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


class TestMergeGuardExitCode:
    """Command exit code must propagate through the guard."""

    @pytest.mark.parametrize("code", [0, 1, 2, 42])
    def test_propagates_exit_code(self, tmp_path: Path, code: int) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _run_merge_guard(state_dir, [PYTHON, "-c", f"import sys; sys.exit({code})"])
        assert result.returncode == code


class TestMergeGuardCrashRelease:
    """Lock must be released even if the guarded command crashes."""

    def test_lock_released_after_crash(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Run a command that exits non-zero (simulating a crash).
        result = _run_merge_guard(state_dir, [PYTHON, "-c", "import os; os._exit(137)"])
        assert result.returncode == 137

        # The lock must be free — acquiring with LOCK_NB must succeed.
        lock_path = state_dir / "integration.lock"
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Success — lock was released.
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pytest.fail("Lock was NOT released after command crash")
        finally:
            os.close(fd)


class TestMergeGuardDefaultStateDir:
    """Default --state-dir must match prime_supervisor.py convention."""

    def test_default_matches_supervisor(self, tmp_path: Path) -> None:
        # Create a bare git repo to have a known git-common-dir.
        subprocess.run(["git", "init", str(tmp_path / "repo")], check=True, capture_output=True)
        repo = tmp_path / "repo"
        common = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True,
        ).strip()
        expected = Path(common) / "verdict-prime"

        # Use the default_state_dir function from prime_workflow.py directly.
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            from prime_workflow import default_state_dir

            assert default_state_dir(repo) == expected
        finally:
            sys.path.pop(0)
