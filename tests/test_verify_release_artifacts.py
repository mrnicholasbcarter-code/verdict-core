"""Tests for isolated release artifact verification."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_release_artifacts import artifact_pair, executable_path


def test_artifact_pair_accepts_exactly_one_wheel_and_sdist(tmp_path: Path) -> None:
    wheel = tmp_path / "verdict_core-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "verdict_core-0.1.0.tar.gz"
    wheel.touch()
    sdist.touch()

    assert artifact_pair(tmp_path) == (wheel, sdist)


def test_artifact_pair_rejects_duplicate_artifacts(tmp_path: Path) -> None:
    (tmp_path / "first.whl").touch()
    (tmp_path / "second.whl").touch()
    (tmp_path / "verdict.tar.gz").touch()

    with pytest.raises(SystemExit, match="expected exactly one wheel"):
        artifact_pair(tmp_path)


def test_executable_path_uses_virtual_environment_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("scripts.verify_release_artifacts.sys.platform", "linux")
    assert executable_path(tmp_path, "verdict") == tmp_path / "bin" / "verdict"

    monkeypatch.setattr("scripts.verify_release_artifacts.sys.platform", "win32")
    assert executable_path(tmp_path, "verdict") == tmp_path / "Scripts" / "verdict.exe"
