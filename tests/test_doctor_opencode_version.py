"""Doctor OpenCode version check (ADR-053 decision 5; task 6.1).

The adapter is pinned to the exact release, so anything that differs from it,
including a prerelease of the same numbers, is a warning.  A missing binary is
only a note.  The binary is a fake script; the real one is never run.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import time
from pathlib import Path

import pytest

import verdict.orchestration.executor_opencode as executor_module
from verdict import doctor_diagnostics as dd
from verdict.orchestration.executor_opencode import PINNED_VERSION


def _fake_binary(tmp_path: Path, output: str, exit_code: int = 0) -> str:
    script = tmp_path / "fake-opencode"
    script.write_text(f"#!/bin/sh\necho '{output}'\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _section(diag: dd.DoctorDiagnostics) -> tuple[str, str, str]:
    rows = [row for row in diag.sections if row[0] == "OpenCode"]
    assert len(rows) == 1, diag.sections
    return rows[0]


@pytest.mark.parametrize("output", [PINNED_VERSION, f"opencode {PINNED_VERSION}"])
def test_matching_version_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    binary = _fake_binary(tmp_path, output)
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: binary)
    diag = dd.DoctorDiagnostics()
    dd._doctor_opencode_version(diag)
    assert _section(diag)[1:] == ("ok", f"v{PINNED_VERSION}")
    assert not diag.warnings


@pytest.mark.parametrize(
    "output",
    [
        "2.4.0",
        "opencode 2.3.1",
        f"{PINNED_VERSION}-rc.1",
        f"opencode {PINNED_VERSION}-beta.2+build5",
    ],
    ids=["newer", "older", "prerelease", "prerelease-with-build"],
)
def test_any_other_version_warns_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    """A prerelease of the pinned numbers is not the pinned release."""
    binary = _fake_binary(tmp_path, output)
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: binary)
    diag = dd.DoctorDiagnostics()
    dd._doctor_opencode_version(diag)
    _name, level, message = _section(diag)
    assert level == "warn", (level, message)
    installed = output.split()[-1]
    assert f"installed v{installed}" in message
    assert f"expected v{PINNED_VERSION}" in message
    assert diag.warnings


def test_missing_binary_is_only_a_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: missing)
    diag = dd.DoctorDiagnostics()
    dd._doctor_opencode_version(diag)
    assert _section(diag)[1] == "note"
    assert not diag.warnings
    assert os.path.exists(str(tmp_path))


def test_failed_version_command_is_not_reported_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper can print the pinned version and still exit nonzero (for example before
    reporting an initialisation error); that is a failed check, not ``ok``."""
    binary = _fake_binary(tmp_path, PINNED_VERSION, exit_code=3)
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: binary)
    diag = dd.DoctorDiagnostics()
    dd._doctor_opencode_version(diag)
    _name, level, message = _section(diag)
    assert level == "note", (level, message)
    assert "exit" in message and "3" in message
    assert not diag.warnings


def test_timed_out_wrapper_leaves_no_descendant_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``VERDICT_OPENCODE_BIN`` may be a wrapper; a timed-out check must clean up after it.

    The wrapper backgrounds a sleeper that inherits the pipes and then waits.  Doctor must
    return promptly and must not leave that descendant running (``subprocess.run`` kills
    only the direct child).  The check runs in its own process group, killed on timeout.
    """
    pid_file = tmp_path / "descendant.pid"
    script = tmp_path / "wrapper-opencode"
    script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: str(script))
    monkeypatch.setattr(dd, "_OPENCODE_VERSION_TIMEOUT_SECONDS", 1.0)
    descendant = 0
    try:
        started = time.monotonic()
        diag = dd.DoctorDiagnostics()
        dd._doctor_opencode_version(diag)
        elapsed = time.monotonic() - started
        assert elapsed < 10.0, f"doctor was not bounded by its timeout ({elapsed:.1f}s)"
        _name, level, message = _section(diag)
        assert level == "note" and "version check error" in message, (level, message)
        assert pid_file.exists(), "wrapper never started its descendant"
        descendant = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _gone(descendant):
            time.sleep(0.05)
        assert _gone(descendant), f"descendant {descendant} outlived the timed-out check"
    finally:
        if not descendant and pid_file.exists():
            descendant = int(pid_file.read_text().strip() or 0)
        if descendant:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(descendant, signal.SIGKILL)  # never leak the test process


def _wrapper_leaving_detached_descendant(tmp_path: Path, stdout_line: str) -> tuple[str, Path]:
    """A wrapper that backgrounds a sleeper with its output detached, prints, and exits 0."""
    pid_file = tmp_path / "descendant.pid"
    script = tmp_path / "wrapper-opencode"
    script.write_text(
        f"#!/bin/sh\nsleep 30 >/dev/null 2>&1 &\necho $! > {pid_file}\n{stdout_line}\nexit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), pid_file


def _assert_descendant_cleaned_up(pid_file: Path) -> None:
    assert pid_file.exists(), "wrapper never started its descendant"
    descendant = int(pid_file.read_text().strip())
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _gone(descendant):
            time.sleep(0.05)
        assert _gone(descendant), f"descendant {descendant} outlived the finished check"
    finally:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(descendant, signal.SIGKILL)  # never leak the test process


def test_normally_exiting_wrapper_leaves_no_descendant_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup is not only for timeouts: a wrapper can print the version, exit 0 and leave a
    descendant (output detached, so the pipes close) running behind it."""
    binary, pid_file = _wrapper_leaving_detached_descendant(tmp_path, f"echo {PINNED_VERSION}")
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: binary)
    diag = dd.DoctorDiagnostics()
    dd._doctor_opencode_version(diag)
    assert _section(diag)[1:] == ("ok", f"v{PINNED_VERSION}")
    _assert_descendant_cleaned_up(pid_file)


def test_undecodable_output_still_cleans_up_the_wrapper_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoding error from ``communicate()`` must not skip the group kill and the reap."""
    binary, pid_file = _wrapper_leaving_detached_descendant(tmp_path, r"printf '\377\376\375'")
    monkeypatch.setattr(executor_module, "_opencode_binary", lambda *a, **k: binary)
    diag = dd.DoctorDiagnostics()
    dd._doctor_opencode_version(diag)
    _name, level, message = _section(diag)
    assert level == "note" and "version check error" in message, (level, message)
    _assert_descendant_cleaned_up(pid_file)


def _gone(pid: int) -> bool:
    """True when ``pid`` no longer exists, or is only a zombie or dead entry awaiting reaping.

    Reaping can race the probe at any point: ``kill`` may succeed and the ``/proc`` read then
    fail (ENOENT, or ESRCH from an already-open entry).  Such a failure is retried; the next
    ``kill`` then reports the process as gone.
    """
    for _ in range(5):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
        except OSError:
            continue  # raced with reaping: probe again
        return state in {"Z", "X"}
    return False


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError(2, "No such file or directory"), ProcessLookupError(3, "No such process")],
    ids=["enoent", "esrch"],
)
def test_liveness_helper_treats_a_pid_reaped_mid_probe_as_gone(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    """A zombie can be reaped between ``kill(pid, 0)`` and the ``/proc`` read; that is gone.

    Treating the read error as "alive" made the cleanup tests flaky (the first check saw the
    zombie, the next saw it reaped mid-probe, with ESRCH from reading the open entry).  Here
    the first ``kill`` succeeds, the read then fails, and the second ``kill`` reports ESRCH.
    """
    probes = iter([None, ProcessLookupError(3, "No such process")])

    def kill(pid: int, sig: int) -> None:
        outcome = next(probes)
        if outcome is not None:
            raise outcome

    def vanished(self: Path, *args: object, **kwargs: object) -> str:
        raise error

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(Path, "read_text", vanished)
    assert _gone(999_999_999) is True


def test_liveness_helper_does_not_call_a_live_process_gone(tmp_path: Path) -> None:
    """The helper must still see a running process as alive (guards against an always-True stub)."""
    import subprocess

    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert _gone(proc.pid) is False
    finally:
        proc.kill()
        proc.wait()
    assert _gone(proc.pid) is True
