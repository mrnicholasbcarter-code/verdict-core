"""Doctor OpenCode version check (ADR-053 decision 5; task 6.1).

The adapter is pinned to the exact release, so anything that differs from it,
including a prerelease of the same numbers, is a warning.  A missing binary is
only a note.  The binary is a fake script; the real one is never run.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import verdict.orchestration.executor_opencode as executor_module
from verdict import doctor_diagnostics as dd
from verdict.orchestration.executor_opencode import PINNED_VERSION


def _fake_binary(tmp_path: Path, output: str) -> str:
    script = tmp_path / "fake-opencode"
    script.write_text(f"#!/bin/sh\necho '{output}'\n")
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
