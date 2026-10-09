"""Tests for pure repo gate discovery (``verdict.orchestration.repo_gates``)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from verdict.orchestration.repo_gates import RepoGate, describe_gates, discover_repo_gates


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _names(gates: Iterable[RepoGate]) -> list[str]:
    return [g.name for g in gates]


def test_ruff_and_mypy_strict_pyproject(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "pkg"\n\n[tool.ruff]\nline-length = 100\n\n'
        '[tool.mypy]\nstrict = true\nfiles = ["src/pkg"]\n',
    )
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format", "mypy"]
    assert gates[0].argv == ("python", "-m", "ruff", "check", ".")
    assert gates[1].argv == ("python", "-m", "ruff", "format", "--check", ".")
    assert gates[2].argv == ("python", "-m", "mypy", "--strict", "src/pkg")
    assert all(g.source == "pyproject:tool.ruff" for g in gates[:2])
    assert gates[2].source == "pyproject:tool.mypy"
    assert describe_gates(gates)["declared"] is True


def test_ruff_only(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["ruff-check", "ruff-format"]
    assert "mypy" not in _names(gates)


def test_mypy_packages_derived_from_project_name_dir(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    _write(tmp_path / "pyproject.toml", '[project]\nname = "pkg"\n\n[tool.mypy]\n')
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["mypy"]
    assert gates[0].argv == ("python", "-m", "mypy", "pkg")


def test_package_json_scripts(tmp_path: Path) -> None:
    _write(
        tmp_path / "package.json",
        json.dumps(
            {
                "scripts": {
                    "test": "jest",
                    "lint": "eslint .",
                    "format:check": "prettier --check .",
                    "typecheck": "tsc --noEmit",
                }
            }
        ),
    )
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == ["typecheck", "lint", "format:check"]
    assert gates[0].argv == ("npm", "run", "typecheck")
    assert gates[1].argv == ("npm", "run", "lint")
    assert gates[2].argv == ("npm", "run", "format:check")
    assert all(g.source == "package.json:scripts" for g in gates)


def test_both_pyproject_and_package_json(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[tool.ruff]\n\n[tool.mypy]\nfiles = ["pkg"]\n')
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"typecheck": "tsc", "lint": "eslint", "format:check": "prettier"}}),
    )
    gates = discover_repo_gates(tmp_path)
    assert _names(gates) == [
        "ruff-check",
        "ruff-format",
        "mypy",
        "typecheck",
        "lint",
        "format:check",
    ]
    assert gates[2].argv == ("python", "-m", "mypy", "pkg")


def test_nothing_declared(tmp_path: Path) -> None:
    gates = discover_repo_gates(tmp_path)
    assert gates == ()
    assert describe_gates(gates) == {"gates": [], "declared": False, "note": "none declared"}


def test_malformed_pyproject_raises_value_error(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "this is = = not valid toml [[[")
    with pytest.raises(ValueError, match=r"pyproject\.toml"):
        discover_repo_gates(tmp_path)


def test_deterministic_same_output(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml", '[tool.ruff]\n\n[tool.mypy]\nstrict = true\nfiles = ["pkg"]\n'
    )
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"format:check": "fmt", "lint": "lint", "typecheck": "tc"}}),
    )
    first = discover_repo_gates(tmp_path)
    second = discover_repo_gates(tmp_path)
    assert isinstance(first, tuple)
    assert first == second
    assert describe_gates(first) == describe_gates(second)
